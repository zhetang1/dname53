import json
import uuid
import ipaddress
import concurrent.futures
import urllib.request
import urllib.error
from datetime import datetime, timezone
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
ddb = boto3.resource("dynamodb", region_name="us-east-1")
searches_table = ddb.Table("dname53-searches")

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


def client_ip(req) -> str:
    # Trust X-Forwarded-For first hop when set (App Runner, ALB, CloudFront)
    xff = req.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return req.remote_addr or ""


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved)


@lru_cache(maxsize=512)
def geolocate(ip: str) -> dict:
    if not _is_public_ip(ip):
        return {}
    url = f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,lat,lon,isp,org,query"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("status") != "success":
            return {}
        return {
            "city": data.get("city") or "",
            "region": data.get("regionName") or "",
            "country": data.get("country") or "",
            "lat": data.get("lat"),
            "lon": data.get("lon"),
            "org": data.get("org") or data.get("isp") or "",
        }
    except (urllib.error.URLError, TimeoutError, ValueError):
        return {}


def log_search(description: str, req, results: list[dict] | None = None) -> None:
    ip = client_ip(req)
    geo = geolocate(ip) if ip else {}
    item = {
        "id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "description": description,
        "ip": ip,
        "user_agent": req.headers.get("User-Agent", ""),
        "referer": req.headers.get("Referer", ""),
        "accept_language": req.headers.get("Accept-Language", ""),
    }
    if results is not None:
        item["results_json"] = json.dumps(results)
        item["available_count"] = sum(1 for r in results if r.get("available"))
        item["total_count"] = len(results)
    # Only include geo fields when present (DDB rejects empty strings sometimes; lat/lon must be Decimal)
    if geo.get("city"):
        item["city"] = geo["city"]
    if geo.get("region"):
        item["region"] = geo["region"]
    if geo.get("country"):
        item["country"] = geo["country"]
    if geo.get("org"):
        item["org"] = geo["org"]
    if geo.get("lat") is not None and geo.get("lon") is not None:
        # boto3 resource interface accepts Decimal; pass as strings to avoid float precision issues
        from decimal import Decimal
        item["lat"] = Decimal(str(geo["lat"]))
        item["lon"] = Decimal(str(geo["lon"]))
    try:
        searches_table.put_item(Item=item)
    except ClientError:
        pass


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


@app.route("/searches")
def searches_page():
    return render_template("searches.html")


@app.route("/api/searches")
def list_searches():
    try:
        items = []
        kwargs = {}
        while True:
            resp = searches_table.scan(**kwargs)
            items.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        for it in items:
            for k in ("lat", "lon"):
                if k in it:
                    it[k] = float(it[k])
            for k in ("available_count", "total_count"):
                if k in it:
                    it[k] = int(it[k])
            if "results_json" in it:
                try:
                    it["results"] = json.loads(it.pop("results_json"))
                except (ValueError, TypeError):
                    it.pop("results_json", None)
        return jsonify({"searches": items, "count": len(items)})
    except ClientError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/suggest", methods=["POST"])
def suggest():
    data = request.get_json(silent=True) or {}
    description = data.get("description", "").strip()

    if not description:
        return jsonify({"error": "Description is required"}), 400

    try:
        domains = suggest_domains_claude(description)

        # Check all domains concurrently (cap workers to respect AWS rate limits)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(check_domain, domains))

        # Available domains first, then alphabetical within each group
        results.sort(key=lambda x: (not x["available"], x["domain"]))

        log_search(description, request, results)
        count = increment_search_count()
        return jsonify({"domains": results, "search_count": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5001)
