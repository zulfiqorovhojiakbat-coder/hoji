# AI Agent Office

12 ta AI agent ishlaydigan ofis: vazifani yozasiz ("kofexona uchun sayt yasa"), agentlar uni bo'lib oladi,
kod yozadi, brauzerda tekshiradi, xatolarni tuzatadi va tayyor saytni shu sahifada jonli ishga tushiradi.

## Agentlar

| Agent | Vazifasi |
| --- | --- |
| Direktor | Vazifani tahlil qiladi, jamoaga topshiriq beradi |
| Tahlilchi | Texnik topshiriq (`docs/spec.md`) |
| Arxitektor | Fayl tuzilmasi, API shartnomasi, backend kerakmi-yo'qmi |
| Dizayner | Dizayn tizimi (`docs/design.md`) |
| Kontent yozuvchi | Sayt matnlari (`docs/content.md`) |
| Baza muhandisi | SQLite sxemasi (`schema.sql`) |
| Backend dasturchi | API (`api.py`) |
| Frontend dasturchi | HTML / CSS / JavaScript |
| Xato qidiruvchi | Statik tekshiruv, headless Chrome konsoli, API sinovi, kod ko'rigi |
| Xavfsizlik auditori | XSS, SQL in'eksiya va boshqa zaifliklar |
| SEO optimizator | Meta teglar, tezlik, qulaylik |
| Deployer | README, yakuniy versiya, ZIP |

Qurilgandan keyin "O'zgartirish so'rang" maydoni orqali istalgan tuzatishni so'rash mumkin — Direktor uni
kerakli agentlarga yo'naltiradi.

## Ishga tushirish

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m office --port 8000
```

`http://localhost:8000` ni oching va **AI sozlamalari** orqali kalit kiriting:

- **Google Gemini** — [AI Studio](https://aistudio.google.com/apikey) kaliti. Modellar avtomatik aniqlanadi;
  band yoki limiti tugagan model o'rniga keyingisi ishlatiladi.
- **OpenAI-mos** — OpenAI, OpenRouter, Groq va h.k. (kalit + base URL + model nomi).

Kalitni muhit o'zgaruvchisi bilan ham berish mumkin: `GEMINI_API_KEY`, yoki `OFFICE_PROVIDER=openai`,
`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OFFICE_MODELS`. Kalit va loyihalar `data/` papkasida saqlanadi (gitga kirmaydi).

Brauzer tekshiruvi uchun tizimda Google Chrome yoki Chromium bo'lishi kerak; bo'lmasa faqat statik tekshiruv ishlaydi.

## Qanday ishlaydi

- `office/orchestrator.py` — quvur: reja → spec → arxitektura/dizayn/matn → baza/backend/frontend → tekshiruv →
  tuzatish → xavfsizlik → SEO → yakuniy tekshiruv → nashr.
- `office/agents.py` — 12 agent, ularning ko'rsatmalari va qaysi fayllarni yozishga ruxsati borligi.
- `office/llm.py` — Gemini / OpenAI-mos oqimli mijoz, modellar orasida zaxira va kutish.
- `office/checks.py` — statik, brauzer va API tekshiruvlari.
- `office/runtime.py` — yaratilgan backendni alohida jarayonda ishga tushiradi. `api.py` faqat ruxsat etilgan
  standart kutubxona modullaridan foydalana oladi.
- `web/` — ofis interfeysi (build bosqichisiz HTML/CSS/JS).

Yaratilgan sayt statik fayllardan iborat; backend kerak bo'lsa `python3 server.py` bilan mustaqil ishlaydi
(ZIP ichida hammasi bor).

## Sinov

Haqiqiy kalitsiz butun quvurni soxta model bilan tekshirish:

```bash
.venv/bin/python tests/mock_llm.py 9911 &
OFFICE_PROVIDER=openai OPENAI_API_KEY=x OPENAI_BASE_URL=http://127.0.0.1:9911/v1 OFFICE_MODELS=mock \
  OFFICE_DATA_DIR=/tmp/office-test .venv/bin/python -m office --port 8765
```

## Cheklovlar

- Ofisda login yo'q — uni ochiq internetga himoyasiz chiqarmang.
- Yaratilgan sayt ofis bilan bir xil manzilda ko'rsatiladi; AI yozgan kod ishonchli deb qabul qilinadi.
