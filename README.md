# Turkiston Daily

Telegram ichida ochiladigan kunlik reja va shaxsiy moliya mini-ilovasi. Ilova rejalar, eslatmalar, takroriy vazifalar, kirim-chiqim, oylik budjet, jamg'arma maqsadlari va kunlik hisobotlarni SQLite bazasida saqlaydi.

## CMD'da ishga tushirish

1. Loyiha papkasidagi `.env` faylini ochib `BOT_TOKEN=YOUR_BOT_TOKEN_HERE` qatoridagi qiymatni BotFather tokeniga almashtiring. Tokenni qo'shtirnoqsiz kiriting va hech kimga yubormang.
2. `start_bot.bat` faylini ikki marta bosing. Birinchi marta virtual muhit yaratilib, kerakli paketlar o'rnatiladi.
3. Bot va mini-ilova lokal kompyuterda `http://127.0.0.1:8001` manzilida ishlaydi. Botga Telegram'da `/start` yuboring.

Portni yoki HTTPS Mini App manzilini o'zgartirish uchun `.env` ichidagi `PORT` va `WEBAPP_URL` qiymatlarini tahrir qiling. Boshqa loyiha bilan port to'qnashmasligi uchun ushbu loyihada `PORT=8001` sozlangan.

## Telegram bot

BotFather orqali bot tokeni oling va uni `.env` fayliga kiriting. `/excel week`, `/excel month`, `/excel year` va `/excel all` buyruqlari davrlar bo'yicha Excel hisobot yuboradi; `/excel` oylik hisobot yuboradi. Mini App tugmasini Telegram ichida ochish uchun `.env` dagi `WEBAPP_URL` ga ommaviy HTTPS tunnel yoki hostingning to'liq manzilini yozing. `http://127.0.0.1:8001` faqat shu kompyuterda ochiladi va Telegram Mini App uchun ishlamaydi. `WEBAPP_URL` ni o'zgartirgach botni qayta ishga tushiring. Python 3.10-3.14 tavsiya etiladi; Telegram paketi `requirements-bot.txt` orqali o'rnatiladi. Token sozlanganda API Telegram `initData` imzosini tekshiradi.

Mini-ilovadagi Excel tugmasi haftalik, oylik, yillik, barcha davr yoki tanlangan sana oralig'i bo'yicha hisobotni yuklaydi.

## Render'ga joylashtirish

1. Loyihani GitHub'dagi repozitoriyga yuklang. `.gitignore` `.env`, virtual muhit va lokal SQLite bazasini repozitoriydan chiqarib tashlaydi.
2. Render'da **New > Blueprint** tanlab, repozitoriyga ulang. `render.yaml` bot va mini-ilovani bir web service sifatida ishga tushiradi.
3. So'ralganda `BOT_TOKEN` ni Render'ning maxfiy Environment Variables maydoniga kiriting; tokenni repozitoriyga yoki chatga joylamang.
4. Blueprint `DATABASE_PATH=/var/data/turkiston_daily.db` va doimiy diskni sozlaydi. Doimiy disk uchun Render'ning pullik Starter rejasi kerak.
5. Deploy tugagach, Render bergan `https://...onrender.com` manzilini oching va Telegram'da botga `/start` yuboring. Render URL'si avtomatik ishlatilgani uchun mini-ilova tugmasi ko'rinadi.

Lokal `turkiston_daily.db` bazasi hostingga avtomatik ko'chmaydi; eski yozuvlar kerak bo'lsa, deploydan oldin ularni alohida ko'chirish kerak.

## Funksiyalar

- Sana bo'yicha reja qo'shish, bajarilgan deb belgilash, nomini o'zgartirish va o'chirish.
- Muhimlik, kunlik/haftalik/oylik takrorlanish va Telegram eslatmasi.
- Kirim va chiqimlarni kategoriya, sana va izoh bilan yuritish; tahrirlash va o'chirish.
- Kunlik hamda oylik balans, kategoriya kesimida xarajatlar va oylik budjet limitlari.
- Jamg'arma maqsadi, muddat va yig'ilgan summani kuzatish.
- Kun yakuni qaydi, baho va oy bo'yicha tarix.
- Mini App'dan yoki Telegram botdan olish mumkin bo'lgan, grafiklar va formatlangan jadvallar bilan 6 varaqlik Excel hisoboti.

Ma'lumotlar `turkiston_daily.db` faylida saqlanadi. Boshqa joyga yozish uchun `DATABASE_PATH` muhit o'zgaruvchisini belgilang.
Telegram bot ma'lumotlar bazasi emas: yozuvlar serverdagi SQLite bazasida foydalanuvchi Telegram ID'si bo'yicha ajratib saqlanadi. `/excel all` shu yozuvlarning to'liq nusxasini Telegram chatiga yuboradi. Hostingda doimiy disk yoki tashqi SQLite/PostgreSQL bazasini ulang; vaqtinchalik fayl tizimli hostingda ma'lumotlar qayta ishga tushganda yo'qolishi mumkin.