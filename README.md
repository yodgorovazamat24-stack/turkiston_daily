# Turkiston Daily

Telegram ichida ochiladigan kunlik reja va shaxsiy moliya mini-ilovasi. Ilova rejalar, eslatmalar, takroriy vazifalar, kirim-chiqim, oylik budjet, jamg'arma maqsadlari va kunlik hisobotlarni SQLite bazasida saqlaydi.

## CMD'da ishga tushirish

1. Loyiha papkasidagi `.env` faylini ochib `BOT_TOKEN=YOUR_BOT_TOKEN_HERE` qatoridagi qiymatni BotFather tokeniga almashtiring. Tokenni qo'shtirnoqsiz kiriting va hech kimga yubormang.
2. `start_bot.bat` faylini ikki marta bosing. Birinchi marta virtual muhit yaratilib, kerakli paketlar o'rnatiladi.
3. `start_bot.bat` botni, Mini App uchun vaqtinchalik HTTPS tunnelni va Windows Startup uchun ishga tushirishni sozlaydi. Telegram'da `/start` yuboring; Mini App tugmasi chiroyli reja va moliya interfeysini ochadi.

Portni yoki HTTPS Mini App manzilini o'zgartirish uchun `.env` ichidagi `PORT` va `WEBAPP_URL` qiymatlarini tahrir qiling. Boshqa loyiha bilan port to'qnashmasligi uchun ushbu loyihada `PORT=8001` sozlangan.

Bot profilida aloqa uchun **@yodgorov_life** ko'rsatiladi. Admin panelga kirish huquqini berish uchun `.env` ga `ADMIN_USER_IDS=123456789,987654321` ko'rinishida adminlarning raqamli Telegram ID'larini kiriting; username bu ID o'rnini bosa olmaydi. Admin botdagi `/admin` yoki pastki paneldagi **Admin panel** tugmasidan foydalanadi. Panel ichidagi tugmalar statistika, foydalanuvchilar ro'yxati, ID bo'yicha qidiruv, tizim holati va qo'llanmani ochadi. Foydalanuvchi ID'si bo'yicha faollik sanalari va yozuvlar soni ko'rsatiladi, shaxsiy yozuvlar matni ochilmaydi. E'lon matni oldindan ko'rsatiladi va faqat admin tasdiqlagandan keyin barcha foydalanuvchilarga yuboriladi; `/cancel` bilan bekor qilish mumkin. Shu amallarning qisqa buyruqlari ham mavjud: `/stat`, `/users`, `/user ID`, `/health`, `/broadcast matn`, `/cancel`.

## Telegram bot

BotFather orqali bot tokeni oling va uni `.env` fayliga kiriting. Mahalliy ishga tushirishda `start_bot.bat` Cloudflare Quick Tunnel orqali HTTPS manzil yaratadi va botni shu manzil bilan ishga tushiradi. Ilovaning bazasi kompyuterdagi `turkiston_daily.db` faylida qoladi; ma'lumotlar bazasi hostingga ko'chirilmaydi. Telegram so'rovlari HTTPS tunnel orqali Cloudflare tarmog'idan o'tadi. Telegram'da `/start` yuboring: javobda Mini App'ni ochish uchun bitta tugma paydo bo'ladi, pastdagi Telegram menyusiga ham shu ilovaning tugmasi qo'shiladi. Hisobot va Excel yuklab olish Mini App'ning **Hisobot** bo'limida.

Bot ishga tushganda nomi, tavsifi, buyruq izohlari va `assets/bot-avatar.png` profil rasmi Telegram'da yangilanadi. Reja, kirim-chiqim, budjet va jamg'arma maqsadlarini Mini App ichida barcha maydonlari bilan tahrirlash mumkin.

Quick Tunnel manzili kompyuter bot bilan qayta ishga tushganda o'zgaradi. Har safar Windows'ga kirgandan so'ng Telegram'da `/start` yuboring va yangi Mini App tugmasini bosing. Tunnel — bepul sinov yechimi, doimiy ishlash kafolati yo'q; internet va kompyuter yoqilgan bo'lishi kerak. Telegram ma'lumotlariga ega Mini App bo'lmagan tashrif buyuruvchilar API orqali shaxsiy yozuvlarni o'qiy olmaydi. Python 3.10-3.14 tavsiya etiladi; Telegram paketi `requirements-bot.txt` orqali o'rnatiladi.

`WEBAPP_URL` ga o'zingiz boshqaradigan doimiy HTTPS manzilni yozish orqali Quick Tunnel o'rniga hosting yoki o'zingizning tunnel manzilingizdan foydalanishingiz mumkin. Botni qayta ishga tushiring.

Xohlasangiz, Telegram chatida ham kunlik reja va pul amaliyotlarini yuritish mumkin:

- `/reja 18:30 Kitob o'qish` — bajarish vaqtini belgilaydi va shu vaqtda eslatadi. `/reja Kitob o'qish` eslatmasiz reja qo'shadi.
- `/bugun` — bugungi rejalar, kirim, chiqim va balansni ko'rsatadi.
- `/kirim 50000 Maosh` — kirim yozadi; `/daromad` ham shu buyruqning taxallusi.
- `/chiqim 25000 Oziq-ovqat` — chiqim yozadi.
- `/hisobot day`, `/hisobot week` yoki `/hisobot month` — kunlik, haftalik yoki oylik hisobotni botga chiqaradi.
- `/buyruqlar` — mavjud buyruqlarni ko'rsatadi. `/start` dan keyin botning pastki panelidagi tugmalarni ham ishlatish mumkin.

Belgilangan vaqtda bot reja nomi, 5 daqiqalik kichik qadam bilan boshlashga undovchi matn va Mini App'ni ochish tugmasi bilan eslatma yuboradi. Muhim deb belgilangan rejalarda matn shu ustuvorlikni eslatadi. Eslatma ayblash yoki bosim qilish uchun emas: hozir qulay bo'lmasa, reja vaqtini o'zgartirish mumkin. Xabar ovozi Telegram va telefonning bildirishnoma sozlamalariga bog'liq; bot alohida yoki majburiy ovozni tanlay olmaydi.

Mini App'da reja eslatmasini **har soatda, reja bajarilguncha** takrorlashni tanlash mumkin. Eslatma xabaridagi **Bajarildi** yoki **Bajarilmadi** tugmalari bilan holatni belgilang; bajarilmagan bo'lsa, sababini yozing. Ilova reja vaqtiga nisbatan kechikish daqiqalarini hisoblaydi va shu nomdagi keyingi reja eslatmasida o'tgan safargi kechikishni eslatadi.

Mini-ilovaning **Hisobot** bo'limida kunlik, haftalik va oylik ko'rsatkichlarni ko'rish, **Hisobotni Telegram botga yuborish** tugmasi bilan xulosani chatga jo'natish mumkin. To'liq Excel faylini shu bo'limdagi **Excel'ni Telegram botga yuborish** tugmasi bilan bot chatiga jo'nating yoki **Qurilmaga yuklab olish** tugmasi bilan saqlang. Excel davri kunlik, haftalik, oylik, yillik, barcha davr yoki tanlangan sana oralig'i bo'lishi mumkin.

Rejaga bajarish vaqtini belgilash mumkin; shu payt uchun Telegram eslatmasi avtomatik yaratiladi, istasangiz boshqa eslatma vaqtini ham tanlaysiz. Vaqti o'tgan, ammo bajarilmagan reja yopilmaydi yoki bajarildi deb belgilanmaydi — u “Bajarilmadi · vaqti o'tdi” deb ko'rsatiladi va qayta rejalashtirilishi mumkin.

Umumiy ko'rinishda o'z yozuvlaringizdan hisoblangan takliflar ko'rinadi: muddati o'tgan rejani ko'chirish, xarajat toifasi uchun qisqa tahlil rejasini qo'shish, oxirgi 30 kunlik xarajatlar asosida yumshoq limit qo'yish va yozuvlar mos kelsa uch oylik zaxira maqsadini yaratish. Takliflar avtomatik qo'llanmaydi; har birini o'zingiz tasdiqlaysiz. Hisoblash faqat shaxsiy yozuvlaringiz doirasida amalga oshadi.

Har bir Telegram foydalanuvchisining reja, hisobot va moliya yozuvlari Telegram ID bo'yicha ajratiladi. Boshqa odam botni ochsa, u o'zining bo'sh shaxsiy ish joyidan boshlaydi; sizning yozuvlaringiz boshqa akkauntlarga ko'rinmaydi.

## Render'ga joylashtirish

1. Loyihani GitHub'dagi repozitoriyga yuklang. `.gitignore` `.env`, virtual muhit va lokal SQLite bazasini repozitoriydan chiqarib tashlaydi.
2. Render'da **New > Blueprint** tanlab, repozitoriyga ulang. `render.yaml` bot va mini-ilovani bir web service sifatida ishga tushiradi.
3. So'ralganda `BOT_TOKEN` ni Render'ning maxfiy Environment Variables maydoniga kiriting; tokenni repozitoriyga yoki chatga joylamang.
4. Blueprint `DATABASE_PATH=/var/data/turkiston_daily.db` va doimiy diskni sozlaydi. Doimiy disk uchun Render'ning pullik Starter rejasi kerak.
5. Deploy tugagach, Render bergan `https://...onrender.com` manzilini oching va Telegram'da botga `/start` yuboring. Render URL'si avtomatik ishlatilgani uchun mini-ilova tugmasi ko'rinadi.

Lokal `turkiston_daily.db` bazasi hostingga avtomatik ko'chmaydi; eski yozuvlar kerak bo'lsa, deploydan oldin ularni alohida ko'chirish kerak.

## Funksiyalar

- Sana bo'yicha reja qo'shish, bajarilgan deb belgilash, barcha maydonlarini tahrirlash va o'chirish.
- Reja bajarish vaqti, alohida eslatma, bajarildi/bajarilmagan holati, muhimlik va kunlik/haftalik/oylik takrorlanish.
- Har soat takrorlanadigan eslatma, bajarilmagan reja sababi va hisoblangan kechikish; keyingi eslatmada oldingi kechikish haqida xabar.
- Kichik qadamdan boshlashga undovchi, rejangizga to'g'ridan-to'g'ri olib boradigan Telegram eslatmalari va kunlik motivatsion yo'naltirish.
- Shaxsiy yozuvlar asosida tasdiqlab qabul qilinadigan reja, budjet va jamg'arma takliflari.
- Kirim va chiqimlarni kategoriya, sana va izoh bilan yuritish; tahrirlash va o'chirish.
- Kunlik hamda oylik balans, kategoriya kesimida xarajatlar va oylik budjet limitlari.
- Jamg'arma maqsadi, muddat va yig'ilgan summani kuzatish.
- Kun yakuni qaydi, baho va oy bo'yicha tarix.
- Mini App'dagi kunlik/haftalik/oylik hisobotlar va botga hisobot yuborish; adminlar uchun bot statistik paneli.
- Mini App'dan yoki Telegram botdan olish mumkin bo'lgan, grafiklar va formatlangan jadvallar bilan 6 varaqlik Excel hisoboti.

Ma'lumotlar `turkiston_daily.db` faylida saqlanadi. Boshqa joyga yozish uchun `DATABASE_PATH` muhit o'zgaruvchisini belgilang.
Telegram bot ma'lumotlar bazasi emas: yozuvlar serverdagi SQLite bazasida foydalanuvchi Telegram ID'si bo'yicha ajratib saqlanadi. `/excel all` shu yozuvlarning to'liq nusxasini Telegram chatiga yuboradi. Hostingda doimiy disk yoki tashqi SQLite/PostgreSQL bazasini ulang; vaqtinchalik fayl tizimli hostingda ma'lumotlar qayta ishga tushganda yo'qolishi mumkin.
