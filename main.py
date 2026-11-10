import os
import staypresent

# Render-এর জন্য একটা ছোট HTTP সার্ভার (health check)
staypresent.web.json({"status": "running"})

# আপনার আসল bot.py চালু করো
staypresent.run(
    "bot.py",
    port=int(os.getenv("PORT", 8080))
)
