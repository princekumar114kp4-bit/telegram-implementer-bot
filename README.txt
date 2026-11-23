Implementer bot - kaise chalana hai

Ye bot aapki bot ki .py file leti hai aur usme do cheez jod sakti hai:
- Web server (Render ko open port chahiye, isliye "/" aur "/health" par "ok" aata hai)
- Pinger (RENDER_EXTERNAL_URL par har kuch minute me ping karta hai, taki bot so na jaye)

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

Buttons:
- "Web server add karo": web server jodta hai. Agar file me pehle se hai to nahi jodta.
- "Pinger add karo": pinger jodta hai (default 5 minute). Pinger ke liye web server zaroori hai,
  isliye Render par "Dono add karo" chuniye.
- "Dono add karo": dono jodta hai, jo pehle se hai use chhod deta hai.

Important:
- Button ka jawab ek message hota hai, popup nahi. Pehle se hai to message me likha aayega.
- Aapki original file nahi badalti. Bot ek nayi file bhejta hai, jiska naam "-updated.py" par khatam hota hai.
- Nayi file ko Render par aap khud dobara deploy karna hoga.
- Bot ki apni file me bhi web server aur pinger hai, isliye use wapas bhejne par wo dobara code nahi jodta.

Note: Ye bot sirf OWNER_ID wale user ke liye hai. Doosre log ko "Ye bot private hai." jawab milega.
