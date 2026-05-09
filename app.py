import json
import concurrent.futures
from functools import lru_cache
from flask import Flask, render_template, request, jsonify
import anthropic
import boto3
from botocore.exceptions import ClientError

app = Flask(__name__)

claude_client = anthropic.Anthropic()
# Route 53 Domains API is only available in us-east-1
r53 = boto3.client("route53domains", region_name="us-east-1")
ssm = boto3.client("ssm", region_name="us-east-1")

SSM_KEY = "/dname53/search-count"


def get_search_count() -> int:
    try:
        resp = ssm.get_parameter(Name=SSM_KEY)
        return int(resp["Parameter"]["Value"])
    except Exception:
        return 0


def increment_search_count() -> int:
    try:
        count = get_search_count() + 1
        ssm.put_parameter(Name=SSM_KEY, Value=str(count), Type="String", Overwrite=True)
        return count
    except Exception:
        return 0


def suggest_domains_claude(description: str) -> list[str]:
    response = claude_client.messages.create(
        model="claude-opus-4-7",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Generate 30 creative, memorable domain name suggestions for:\n\n{description}\n\n"
                    "Rules:\n"
                    "- Mix of .com, .net, .org, .io, .co, .app TLDs\n"
                    "- Short, brandable names (ideally under 15 chars before the TLD)\n"
                    "- No hyphens, all lowercase\n\n"
                    "Return ONLY a JSON array — no explanation, no markdown:\n"
                    '["example.com", "mybrand.io"]'
                ),
            }
        ],
    )

    text = next((b.text for b in response.content if b.type == "text"), "")
    start, end = text.find("["), text.rfind("]") + 1
    if start >= 0 and end > start:
        return json.loads(text[start:end])
    return json.loads(text)


@lru_cache(maxsize=128)
def get_tld_price(tld: str) -> dict | None:
    """Fetch Route 53 registration and renewal price for a TLD. Result is cached."""
    try:
        resp = r53.list_prices(Tld=tld.lstrip("."))
        prices = resp.get("Prices", [])
        if not prices:
            return None
        p = prices[0]
        reg = p.get("RegistrationPrice", {})
        ren = p.get("RenewalPrice", {})
        return {
            "registration": reg.get("Price"),
            "renewal": ren.get("Price"),
            "currency": reg.get("Currency", "USD"),
        }
    except ClientError:
        return None


def check_domain(domain: str) -> dict:
    """Check availability on Route 53 and attach pricing for available domains."""
    try:
        resp = r53.check_domain_availability(DomainName=domain)
        availability = resp["Availability"]
        is_available = availability == "AVAILABLE"

        result = {
            "domain": domain,
            "available": is_available,
            "status": availability,
        }

        if is_available:
            tld = domain.rsplit(".", 1)[-1]
            result["price"] = get_tld_price(tld)

        return result
    except ClientError as e:
        return {
            "domain": domain,
            "available": False,
            "status": "ERROR",
            "error": e.response["Error"]["Message"],
        }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stats")
def stats():
    return jsonify({"search_count": get_search_count()})


@app.route("/api/suggest", methods=["POST"])
def suggest():
    data = request.get_json(silent=True) or {}
    description = data.get("description", "").strip()

    if not description:
        return jsonify({"error": "Description is required"}), 400

    try:
        domains = suggest_domains_claude(description)
    except Exception as e:
        return jsonify({"error": f"AI suggestion failed: {e}"}), 500

    # Check all domains concurrently (cap workers to respect AWS rate limits)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(check_domain, domains))

    # Available domains first, then alphabetical within each group
    results.sort(key=lambda x: (not x["available"], x["domain"]))

    count = increment_search_count()
    return jsonify({"domains": results, "search_count": count})


if __name__ == "__main__":
    app.run(debug=True, port=5001)
