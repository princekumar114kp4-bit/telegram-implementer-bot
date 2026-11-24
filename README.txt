Implementer bot - kaise chalana hai

Ye bot aapki bot ki .py file leti hai aur usme do cheez jod sakti hai:
- Web server (Render ko open port chahiye, isliye "/" aur "/health" par "ok" aata hai)
- Pinger (RENDER_EXTERNAL_URL par har kuch minute me ping karta hai, taki bot so na jaye)
Bade files ke liye AI sirf kaam ke hisse dekhta hai, limit 1 MB.

Ye bot khud bhi yahi dono cheez karta hai: iska apna web server hai aur ye apne aap ko
ping karta hai, taki Render ke free plan par ye bot bhi so na jaye.

Render par setup:
1. Is bot ko Render par ek Web Service ke roop me deploy karein (free plan chal jayega).
   Web Service isliye, kyunki Render sirf web service ko PORT aur RENDER_EXTERNAL_URL deta hai.
   Build command: pip install -r requirements.txt
   Start command: python implementer_bot.py
   Health check path (agar poocha jaye): /health
2. Environment variables:
   BOT_TOKEN           = aapke bot ka token (BotFather se)
   OWNER_ID            = aapka numeric Telegram ID (bot ko /start bhejkar ID dikh jaayegi)
   DATA_DIR            = (optional) jahan files save hon
   PORT                = (optional) Render khud deta hai; na ho to 10000 use hota hai
   RENDER_EXTERNAL_URL = Render khud deta hai (service ka address). Ye na ho to self-ping
                         skip hota hai, bot phir bhi chalta hai aur log me ek line aati hai.
   PING_MINUTES        = (optional) kitne minute me self-ping ho, 1 se 60 tak (default 5)
3. Bot ko Telegram par /start bhejein, phir apni bot ki .py file bhejein.

Buttons (owner ke liye, neeche ke keyboard me):
- "Web server add karo", "Pinger add karo", "Dono add karo": web server, pinger ya dono jodta hai.
  Pinger ke liye web server zaroori hai, isliye Render par "Dono add karo" chuniye.
AI key set ho to web/pinger options pehle purana web server/pinger code hata ke naya jodte hain.
- "Database Export/Import": owner/admin ke liye backup (export) aur restore (import). Import ke baad bot restart karein.
- "AI key set karo": AI key save karta hai. Ye sirf Database Export/Import ke liye chahiye.
  Render par AI_API_KEY env variable se bhi de sakte hain (optional).
- OpenRouter aur Groq ki keys bhi chalti hain, "AI key set karo" se daalein.
- /aimodel likh kar AI ka model badal sakte hain.
- "Status dekho" aur "Pichli file lo" bhi keyboard par hain.

Important:
- Button ka jawab ek message hota hai, popup nahi. Pehle se hai to message me likha aayega.
- Aapki original file nahi badalti. Bot ek nayi file bhejta hai, jiska naam "-updated.py" par khatam hota hai.
- Nayi file ko Render par aap khud dobara deploy karna hoga.
- Bot ki apni file me bhi web server aur pinger hai, isliye use wapas bhejne par wo dobara code nahi jodta.

Note: Ye bot sirf OWNER_ID wale user ke liye hai. Doosre log ko "Ye bot private hai." jawab milega.
