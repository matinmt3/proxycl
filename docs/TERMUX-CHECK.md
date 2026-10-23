# بررسی نسخهٔ ۳.۴ روی گوشی در Termux

این راهنما برای آزمون واقعی گوشی است. تست‌های رایانه و `selftest` به‌تنهایی عملکرد تلگرام، بازشدن مرورگر اندروید، مصرف باتری یا ادامهٔ اسکن پس از بسته‌شدن Termux را تأیید نمی‌کنند. برای مراحل زیر ورود حساب تلگرام لازم نیست. پیش از مقایسهٔ نتایج، از همان اینترنت و همان گوشی استفاده کنید.

## نصب نسخهٔ آزمایشی در پوشهٔ جدا

فرمان زیر پس از انتشار شاخهٔ `codex/telegram-v3-4` قابل استفاده است. نصب قبلی در `~/Revmamad` حفظ می‌شود:

```bash
pkg install -y curl
REVMAMAD_BRANCH=codex/telegram-v3-4 \
REVMAMAD_DIR="$HOME/Revmamad-v3.4-check" \
bash <(curl -fsSL https://raw.githubusercontent.com/matinmt3/proxycl/codex/telegram-v3-4/install.sh)
```

پس از بازشدن منو، گزینهٔ **۶** را برای خروج بزنید. سپس:

```bash
cd "$HOME/Revmamad-v3.4-check"
python main.py --version
python main.py selftest --network phone-check
cat output/networks/phone-check/selftest-report.json
```

باید نسخهٔ `3.4.0` و **۱۱ نتیجهٔ PASS** دیده شود: چهار وابستگی `yaml`، `httpx`، `Crypto` و `idna`؛ تنظیمات؛ رمزنگاری؛ خواندن لینک MTProxy؛ بردارهای معلوم HMAC و رفت‌وبرگشت لینک WEB دارای مسیر؛ ذخیره/بازیابی؛ دریافت واقعی فایل‌های داشبورد محلی؛ و شروع/توقف یک کار آزمایشی محلی با محافظت token و Origin.

`all_passed: true` فقط آزمون آفلاین و ارتباط `127.0.0.1` را تأیید می‌کند. این کار آزمایشی هیچ پروکسی واقعی را تست نمی‌کند. فیلد `termux` صرفاً محیط اجرای Termux را از نشانه‌های محیط تشخیص می‌دهد؛ تأیید عملکرد فیزیکی گوشی نیست. اگر وابستگی کم باشد، راهنمای نصب `requirements-lite.txt` نمایش داده می‌شود؛ پس از رفع خطا دوباره `selftest` را اجرا کنید.

## اسکن زمان‌دار، فشار گوشی و ده نتیجهٔ برتر

نام `phone-check` یک برچسب انتخابی است؛ برنامه نوع سیم‌کارت یا وای‌فای را خودکار تشخیص نمی‌دهد. برای اینترنت‌های متفاوت نام‌های جدا مانند `wifi` و `mobile` انتخاب کنید و همان نام را در فرمان‌های بعدی تکرار کنید.

```bash
python main.py best10 --mode mtproto --minutes 3 --network phone-check
```

مراحل `PREFILTER`، `BASELINE` و `FINALISTS`، پوشش نامزدها و محدودیت هم‌زمانی را بررسی کنید. بخشی از زمان برای آزمون نهایی نامزدهای بهتر کنار گذاشته می‌شود. رسیدن به زمان تعیین‌شده یعنی پایان یک نوبت محدود؛ لزوماً همهٔ نامزدها بررسی نشده‌اند. پایان و ذخیرهٔ امن ممکن است اندکی پس از مهلت انجام شود. کمتر از ده نتیجه یا صفر نتیجه، اگر نامزدها معیارها را پاس نکنند، خروجی معتبر است؛ برنامه برای تکمیل عدد ده نتیجه نمی‌سازد.

اسکن نامحدود و تعداد دلخواه نیز باقی‌اند:

```bash
python main.py best10 --mode mtproto --count 25 --network phone-check
python main.py best10 --mode mtproto --count ALL --network phone-check
```

در منو، **گزینهٔ ۱۳** زمان ۳، ۱۰، ۲۰ دقیقه یا مقدار دلخواه را تنظیم می‌کند؛ مقدار صفر محدودیت زمانی را برمی‌دارد. این زمان برای آزمون بعدی است. کنترل هم‌زمانی بر اساس فشار محلیِ قابل اندازه‌گیری تنظیم می‌شود؛ سنجش دمای واقعی باتری یا تضمین جلوگیری از گرما نیست. هنگام آزمایش رفتار رابط، پاسخ‌گویی ترمینال و قطع‌نشدن برنامه را بررسی کنید.

## توقف، قطع اینترنت و Resume هوشمند

هنگام اجرای اسکن یک بار **Ctrl+C** بزنید و منتظر ذخیرهٔ نتایج بمانید. نتایجی که آزمون نهایی‌شان تمام نشده باید موقت/`PROVISIONAL` معرفی شوند؛ پوشش محدود نامزدها هم باید مشخص باشد. سپس بدون `--count` ادامه دهید:

```bash
python main.py best10 --mode mtproto --resume --network phone-check
```

نامزدها و ترتیبشان حفظ می‌شوند. با تنظیم پیش‌فرض، اگر نمونهٔ هر نامزد بیش از ۳۰ دقیقه عمر داشته باشد، تمام شواهد آن نامزد کنار گذاشته و دوباره آزمون می‌شود؛ شواهد قدیمی با یک نمونهٔ تازه معتبر نمی‌شوند. خودِ فهرست نامزدها حداکثر **۷ روز از زمان ایجاد اولیه** قابل نگهداری است. تغییر تنظیمات مؤثر در آزمون، خراب‌شدن checkpoint، اتمام کامل اسکن یا عبور از مهلت نگهداری می‌تواند Resume را رد کند؛ در این حالت اسکن تازه اجرا کنید. این ادامه منابع را دوباره دریافت نمی‌کند.

یک بار هم اینترنت گوشی را در میان اسکن قطع و دوباره وصل کنید؛ خطاهای شبکه باید ثبت شوند و برنامه نباید متوقف بماند. پس از پایان ذخیره، Resume یا بازآزمایی را اجرا کنید. برای بررسی بسته‌شدن اجباری، اول با Ctrl+C آزمایش را تمام کنید؛ بعد در اجرای دیگری Termux را ببندید و دوباره باز کنید. ادامه باید شواهد ذخیره‌شده را بخواند؛ آخرین دستهٔ ذخیره‌نشده ممکن است از دست برود. نتیجهٔ این دو آزمایش را جدا ثبت کنید.

## Telegram WEB با HTTP/SOCKS فرق دارد

چهار حالت مستقل وجود دارد:

| حالت | نوع اتصال و لینک |
| --- | --- |
| `mtproto` | MTProxy معمولی و FakeTLS |
| `telegram` | همان آزمون MTProxy با خروجی لینک `t.me/proxy` |
| `web` | HTTP/SOCKS عمومی؛ لینک پروکسی تلگرام نیست |
| `webproxy` | Telegram WEB با HTTPS/WSS و لینک `tg://webproxy` |

هر حالت خروجی و checkpoint جدا دارد. برای Telegram WEB، لینک واقعی را از اپراتور همان سرویس بگیرید و وارد کنید:

```bash
python main.py add-webproxy --link 'tg://webproxy?server=YOUR_WEB_HOST&secret=YOUR_WEB_SECRET'
python main.py best10 --mode webproxy --count 5 --network phone-check
```

مقادیر `YOUR_WEB_HOST` و `YOUR_WEB_SECRET` جای‌نگهدارند و اتصال واقعی نیستند. لینک WEB نام دامنه دارد و پورت آن ۴۴۳ است؛ لینک دارای مسیر به secret با علامت مخصوص `0x70` نیاز دارد. لینک معتبر اپراتور را کامل کپی کنید و secret مسیر را دستی به secret خام تبدیل نکنید. افزودن لینک به‌معنی سالم‌بودن آن نیست؛ باید آزمون را پاس کند.

یک **نسخهٔ تلگرام سازگار با WEB Proxy** لازم است. برای بازکردن نتیجه، ابتدا لینک `tg://webproxy` را امتحان کنید؛ وجود قالب `https://t.me/webproxy` در خروجی به معنی پشتیبانی قطعی صفحهٔ `t.me` یا نسخهٔ نصب‌شدهٔ تلگرام نیست. لینک `t.me/proxy` هم با تغییر نام مسیر به WEB تبدیل نمی‌شود. اگر لینک در تلگرام شناخته نشد، نسخه و منبع نصب تلگرام را ثبت کنید و نتیجهٔ «شناخته‌نشدن لینک» را از «آزمون اتصال ناموفق» جدا گزارش کنید.

آزمون برنامه از حامل Python/OpenSSL استفاده می‌کند؛ اثر انگشت TLS/WebView آن با کلاینت رسمی یکسان نیست. قبولی آن تضمین عبور از DPI روی تمام اینترنت‌ها، سرعت دانلود مدیا یا عملکرد آینده نیست. اگر نتیجهٔ واجد شرایط وجود داشت، روی همین گوشی و اینترنت لینک نخست را باز و ارسال/دریافت واقعی پیام را هم بررسی کنید.

برای HTTP/SOCKS و لینک MTProxy هم جداگانه اجرا کنید:

```bash
python main.py best10 --mode web --count 25 --network phone-check
python main.py best10 --mode telegram --count 25 --network phone-check
```

## داشبورد محلی، کنترل اسکن و تاریخچه

```bash
python main.py dashboard --network phone-check
```

بررسی کنید مرورگر اندروید باز می‌شود یا نه. اگر خودکار باز نشد، آدرس چاپ‌شده، معمولاً `http://127.0.0.1:8000`، را دستی وارد کنید. نتیجه‌های «بازشدن خودکار» و «بازشدن دستی» را جدا گزارش کنید. `selftest` بازشدن مرورگر را بررسی نمی‌کند.

در داشبورد روی همین آدرس محلی، حالت و برچسب شبکه را انتخاب و شروع، توقف، ادامه و بازآزمایی را جدا بررسی کنید. شمارنده‌ها، مرحله، پوشش نامزدها، توضیح رتبه‌ها، کپی لینک و تاریخچه را هم ببینید. کنترلر هم‌زمان یک اسکن را اجرا می‌کند؛ برای شروع دیگری اول توقف و ذخیرهٔ کار جاری را صبر کنید. یک اسکن مستقل در ترمینال دیگر هم‌زمان با کار داشبورد اجرا نکنید. صفحهٔ قدیمی که token آن منقضی شده را تازه‌سازی کنید. با Ctrl+C سرور متوقف و توقف امن کار داشبورد درخواست می‌شود.

کنترل اسکن فقط روی loopback فعال است. برای داشبورد صرفاً خواندنی:

```bash
python main.py dashboard --network phone-check --read-only
```

اگر برای مشاهده از دستگاه دیگری، `dashboard.host` را در تنظیمات به آدرس LAN تغییر دادید، از `--read-only` استفاده کنید؛ برنامه کنترل اسکن را روی LAN فعال نمی‌کند. نتیجه‌ها شامل آدرس و secret عمومی پروکسی هستند؛ سرور مشاهده را فقط در شبکه‌ای که می‌خواهید به این داده‌ها دسترسی داشته باشد باز کنید.

```bash
python main.py history 10 --mode mtproto --network phone-check
python main.py recheck --mode mtproto --network phone-check
```

تاریخچه باید مشاهدات همین برچسب/حالت را نشان دهد. `recheck` حداکثر بیست اتصال ذخیره‌شدهٔ واجد شرایط را تازه تست می‌کند؛ اگر اتصال واجد شرایط ذخیره نشده باشد، پیام درخواست اسکن تازه درست است. ادامهٔ بازآزمایی باید با `recheck --resume` یا انتخاب «بازآزمایی» در Resume منو/داشبورد انجام شود.

## چه چیزی گزارش شود؟

نسخهٔ برنامه، نسخهٔ اندروید، منبع نصب Termux، نسخه/منبع تلگرام، نوع اینترنت، نتیجهٔ ۱۱ بررسی `selftest` و مرحلهٔ بروز خطا را ثبت کنید. برای Resume بنویسید توقف در کدام مرحله بود و پس از ادامه کدام شواهد از نو تست شدند؛ برای WEB نتیجهٔ شناخته‌شدن لینک را هم بگویید. اگر پروکسی اول فهرست در تلگرام خوب نبود، زمان تست و رفتار مشاهده‌شده را ذکر کنید. رمز، کد ورود، توکن کنترل داشبورد یا فایل نشست حساب را ارسال نکنید.

وجود این راهنما به معنی قبولی آزمون گوشی نیست. پس از انتشار نهایی، فرمان نصب معمول از شاخهٔ `main` در README برای نصب/آپدیت عمومی استفاده می‌شود.


## Additional v4 RC acceptance checks (not yet executed on a physical phone)

Record Android, Termux and Telegram versions, device model and your chosen network label. Use a separate RC checkout; retain the stable installation.

1. Fresh install, local dashboard and the three mode families at 320px. Copy and open actual fresh links in the installed Telegram client.
2. Start one long scan; attempt a second CLI/dashboard/scheduler writer on the same output root. Confirm refusal and no damaged snapshot.
3. Ctrl+C, screen off, internet loss, Android force-stop, restart and Resume. Record the last completed sample/checkpoint and expiry labels.
4. Add personal sources/favourites, save settings, update then roll back. Compare source/settings/output checksums and confirm the previous runnable version.
5. With your own locally entered API credentials, test an explicitly chosen public document through up to five freshly qualified proxies. Record media DC migration, observed volume, cap/interrupt statuses and logout confirmation. Do not share credentials or account screenshots.
6. Export doctor diagnostics, inspect the allowlisted report, and attach only that sanitized report with the test outcome.

These boxes remain open until real device evidence is recorded. Host, shim and controlled-relay tests do not fill them automatically.
