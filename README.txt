Implementer bot - kaise chalana hai (bahari service ya API key ki zaroorat nahi)

Ye bot aapki bot ki .py file leti hai aur usme ye cheezein jod sakti hai:
- Web server (Render ko open port chahiye, isliye "/" aur "/health" par "ok" aata hai)
- Pinger (RENDER_EXTERNAL_URL par har kuch minute me ping karta hai, taki bot so na jaye)
- Database Export/Import (owner/admin ke liye backup aur restore, neeche detail me)

Purana web server ya pinger code: agar file me pehle se web server ya pinger hai, to bot use
apne aap pehchanta hai (file ke code ko padh kar, built-in check se). Jo option chuna gaya hai, sirf wahi
purana code hata kar naya code jodta hai. Jo code pakka nahi pehchana gaya, use bot nahi chhedta
aur message me line number batata hai. Hatane se pehle ek check hota hai, aur kuch bhi gadbad ho
to original file waisi hi rehti hai.

Repair pass: naya file banne ke baad bot kuch aam galtiyan apne aap theek karta hai aur report
me likhta hai ki kya badla:
- Token file me likha ho to use hata kar BOT_TOKEN environment variable se lene lagta hai.
  (Purana token surakshit nahi hai, BotFather se naya token banayein.)
- Flask app.run me host 0.0.0.0 aur PORT environment variable.
- Tab ki jagah spaces, Windows line endings (CRLF), BOM, ajeeb space (NBSP), smart quotes.
- Chhuta hua import (os, threading, time) jod deta hai.
Ye sirf report karta hai, badalta nahi: ek se zyada polling line, webhook aur polling dono, async
function me time.sleep. Ye fix bhi wahi hota hai jo file ko toot na de; toota fix apne aap hat jaata hai.

Requirements: file bhejne par bot ek requirements.txt bhi bhejta hai (sirf package ke naam, version
nahi). /check me bhi packages ki list dikhti hai.

Limits:
- Bot ki .py file: 20 MB tak.
- Export zip: 50 MB tak. Import ke liye Telegram bot 20 MB tak ki file hi download kar sakta hai.

Render par setup:
1. Is bot ko Render par ek Web Service ke roop me deploy karein (free plan chal jayega).
   Web Service isliye, kyunki Render sirf web service ko PORT aur RENDER_EXTERNAL_URL deta hai.
   Build command: pip install -r requirements.txt
   Start command: python implementer_bot.py
   Health check path (agar poocha jaye): /health
2. Environment variables:
   BOT_TOKEN           = aapke bot ka token (BotFather se)
   OWNER_ID            = aapka numeric Telegram ID (bot ko /start bhejkar ID dikh jaayegi)
   HYV_ADMIN_IDS       = (optional) backup feature ke liye admin Telegram IDs, comma se alag karke.
                         Ye zaroori hai un bots ke liye jinme owner ka alag variable nahi hota
                         (jaise post maker). Ek hi admin ho to sirf apna ID likhein.
   DATA_DIR            = (optional) jahan files save hon
   PORT                = (optional) Render khud deta hai; na ho to 10000 use hota hai
   RENDER_EXTERNAL_URL = Render khud deta hai (service ka address). Ye na ho to self-ping
                         skip hota hai, bot phir bhi chalta hai aur log me ek line aati hai.
   PING_MINUTES        = (optional) kitne minute me self-ping ho, 1 se 60 tak (default 5)
3. Bot ko Telegram par /start bhejein, phir apni bot ki .py file bhejein.

Buttons (neeche ke keyboard me, owner ke liye):
- "Web server add karo": web server jodta hai. Agar file me pehle se hai to purana hata kar naya jodta hai.
- "Pinger add karo": pinger jodta hai (default 5 minute). Pinger ke liye web server zaroori hai,
  isliye Render par "Dono add karo" chuniye.
- "Dono add karo": dono jodta hai, jo pehle se hai use badal deta hai.
- "Database Export/Import": owner/admin ke liye backup aur restore feature jodta hai (neeche).
- "Status dekho" (neeche ki alag row me, hare rang ka): pichli bheji file me web server aur pinger pehle se
  hain ya nahi, ye dikhata hai.
- "Pichli file lo" (neeche): pichli updated file dobara bhejta hai (agar Telegram par file kho jaye).

/check: "/check web", "/check pinger", "/check both" (ya "pehle check dono") bhejne par bot bataata hai
ki file me kya badlega, par koi file nahi banata aur kuch save nahi karta.

Rang: 4 option buttons aur Database Export/Import button neele (primary), Status dekho aur Pichli file lo
hare (success).
Ye rang Telegram ke naye version me dikhna chahiye. Agar app me rang na dikhe, button phir bhi kaam karta hai.

Typed /status aur /last bhi chalte hain (chupchaap, bina kisi hint ke). Keyboard /start par bhi dikhta hai,
taki buttons file bheje bina bhi mil jaayein.

Database Export/Import (backup):
- Admin ke reply keyboards me "🗄 Database Export/Import" button ab har keyboard me runtime se judta hai
  (sirf admin ke private chat me, sabse neeche wali nayi row, neele rang me). Bot ki kisi bhi menu file ko chhedna nahi padta.
  Purani file me block pehle se hai to implementer dobara chalane par block naye version se badal jaata hai.
- Button dabaane ke baad bot ki file bhejein. Bot us file me backup feature jod deta hai, aur ek nayi
  "-updated.py" file bhejta hai. Ise Render par dobara deploy karein.
- Nayi bot file chalne par admin ko /backup bhejna hai. Menu me 📤 Export, 📥 Import, 🔙 Back aate hain.
- Export: data files ek zip me, saath me manifest (file ki list, size, checksum). Bot ki .py file,
  .env, token/secret/password wali files, __pycache__, venv, .git bahar rehte hain.
- Import: zip bhejein. Bot use check karta hai, phir confirmation poochta hai. Pehle current data
  ki safety copy banti hai (hyv_safety folder me).
- Import ke baad bot ko Render par restart karna zaroori hai, tabhi naya data chalega.
- Ye feature aiogram, pyrogram aur telethon wali bots me nahi jodta. Telebot (pyTelegramBotAPI) aur
  python-telegram-bot v20+ me chalta hai.
- Agar target bot me pehle se /backup command hai, to admin ke liye wo command ye feature badal dega.

Important:
- Button ka jawab ek message hota hai, popup nahi. Pehle se hai to message me likha aayega.
- Aapki original file nahi badalti. Bot ek nayi file bhejta hai, jiska naam "-updated.py" par khatam hota hai.
- Nayi file ko Render par aap khud dobara deploy karna hoga.
- Bot ki apni file me bhi web server aur pinger hai, isliye use wapas bhejne par wo dobara code nahi jodta.

Tests (sab offline, koi network nahi, koi asli token nahi): CHANGES.txt dekhein.

Note: Ye bot sirf OWNER_ID wale user ke liye hai. Doosre log ko "Ye bot private hai." jawab milega.
