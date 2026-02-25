# Simple Free Tier Implementation

## The Plan (3 Steps)

### Step 1: Deploy a Tiny Proxy (5 minutes)

Create a simple server that holds your OpenRouter API key and forwards requests to OpenRouter's `/free` router.

**server.js** (deploy to Vercel/Railway):
```javascript
const express = require('express');
require('dotenv').config();

const app = express();
app.use(express.json());

app.post('/chat', async (req, res) => {
  const response = await fetch('https://openrouter.ai/api/v1/chat/completions', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${process.env.OPENROUTER_API_KEY}`,
      'Content-Type': 'application/json',
      'HTTP-Referer': 'https://github.com/vincentm65/vmCode-CLI',
      'X-Title': 'vmCode'
    },
    body: JSON.stringify({ model: 'openrouter/free', ...req.body })
  });
  res.json(await response.json());
});

app.listen(3000);
```

**Deploy:**
```bash
# Install Vercel CLI
npm install -g vercel

# Create folder and deploy
mkdir vmcode-proxy && cd vmcode-proxy
npm init -y
npm install express dotenv
# Add server.js above
vercel --prod

# Set your OpenRouter API key
vercel env add OPENROUTER_API_KEY production
```

That's it. Your proxy is now at `https://vmcode-proxy.vercel.app`

---

### Step 2: Add vmcode_free Provider (2 minutes)

Add this to your provider registry in `src/llm/config.py`:

```python
_provider_registry_cache = {
    # ... existing providers ...

    "vmcode_free": {
        "type": "api",
        "api_key": "",  # Not needed
        "model": "openrouter/free",
        "api_base": "https://vmcode-proxy.vercel.app",
        "endpoint": "/chat",
        "error_prefix": "vmCode Free",
        "default_temperature": 0.7,
        "default_top_p": 0.9,
    },
}
```

---

### Step 3: Set as Default (1 minute)

Update `config.yaml.example`:
```yaml
LAST_PROVIDER: vmcode_free
```

That's it. When users install vmCode:
- It loads `vmcode_free` by default
- Works immediately (no API key needed)
- Uses OpenRouter's free models through your proxy

---

## Costs

- **Your proxy:** $0 (Vercel free tier)
- **OpenRouter usage:** Pay for what you use
  - Free models are cheap (~$0-0.10 per 1M tokens)
  - For 1000 users: ~$0-10/month initially
- Set billing alerts on OpenRouter to control costs

---

## Optional: Rate Limiting

Add to proxy if you're worried about abuse:

```javascript
const rateLimit = require('express-rate-limit');

const limiter = rateLimit({
  windowMs: 15 * 60 * 1000, // 15 minutes
  max: 100, // 100 requests
});

app.post('/chat', limiter, async (req, res) => {
  // ... same as above
});
```

---

## Testing

```bash
# Test your proxy
curl -X POST https://vmcode-proxy.vercel.app/chat \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Hello"}]}'

# Test vmCode
vmcode
# Should work immediately with free tier
```

---

## Summary

**Time to implement:** ~15 minutes

**What changes:**
1. Deploy a 30-line proxy server
2. Add 10 lines to config.py
3. Change default provider

**Result:**
- Users install and use vmCode immediately
- No setup required
- You control costs via rate limiting

Done.
