# Domain Name Finder

A web app that uses Claude AI to suggest domain names based on your project description, then checks availability and pricing on AWS Route 53 Domains.

## How it works

1. Enter a description of your project or idea
2. Claude generates 30 creative, brandable domain name suggestions
3. Each domain is checked against the Route 53 Domains API in parallel
4. Only available domains are shown, with registration and renewal pricing

## Setup

**Prerequisites:** Python 3.11+, AWS credentials with Route 53 Domains access, Anthropic API key.

```bash
pip install -r requirements.txt
```

Set environment variables:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-1
```

## Run

```bash
python app.py
```

Open [http://localhost:5001](http://localhost:5001).

## Stack

- **Backend:** Flask, Anthropic Python SDK (`claude-opus-4-7`), boto3
- **Frontend:** Vanilla JS, Tailwind CSS
- **AWS:** Route 53 Domains API (`us-east-1` only)
