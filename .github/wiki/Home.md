# JiraStats — Wiki Home — Wiki Home

A lightweight, configuration-driven reporting toolkit for Jira and Confluence. It discovers relevant scope via CQL/JQL, fetches only necessary data, and produces CSV reports and charts on throughput, timelines, and resource usage.

> ☕ [Support NeuralMimicry on Crowdfunder](https://www.crowdfunder.co.uk/p/qr/aWggxwPW?utm_campaign=sharemodal&utm_medium=referral&utm_source=shortlink)

---

## Quick navigation

| Page | Description |
|---|---|
| [Getting Started](Getting-Started) | Build, configure, and run |
| [Contributing](Contributing) | How to raise issues and submit pull requests |

---

## Quick start

```bash
pip install -r requirements.txt
# Edit config.json to point at your Jira/Confluence instance
python run_report.py
```

## Key capabilities

- Discovery-driven JQL refinement — narrows scope before fetching
- Small-batch fetching with JSONL cache fallback
- LLM-backed quality insights (optional)
- Supports Google, Brave, DuckDuckGo, and Tavily search engines



## Get involved

- 🐛 [Report a bug or request a feature](https://github.com/neuralmimicry/jirastats/issues)
- 💬 [Join the discussion](https://github.com/neuralmimicry/jirastats/discussions)
- 📧 Direct support from the founder: [info@neuralmimicry.ai](mailto:info@neuralmimicry.ai) · **£1,000/day + VAT**
- 🌐 [neuralmimicry.ai](https://neuralmimicry.ai)
