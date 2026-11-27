"""The store's own words in Uzbek and Russian, keyed by the English.

English is written where it's used, so the code still reads as before; `tr` looks the
same text up for another language and fills in the {placeholders}. A missing
translation falls back to English rather than failing.
"""

from typing import Literal

Language = Literal["en", "uz", "ru"]
LANGUAGES: tuple[Language, ...] = ("en", "uz", "ru")

UZBEK = {
    # --- Telegram: to the admin ---
    "🛍️ NEW ORDER CONFIRMED #{id}": "🛍️ YANGI BUYURTMA TASDIQLANDI #{id}",
    "👤 Customer: {contact}": "👤 Mijoz: {contact}",
    "📍 Address: {address}": "📍 Manzil: {address}",
    "📦 Items: {items}": "📦 Mahsulotlar: {items}",
    "💰 Total: ${total} via {provider}": "💰 Jami: ${total}, {provider} orqali",
    "💰 Total: ${total}. No payment link was created.": "💰 Jami: ${total}. To'lov havolasi yaratilmagan.",
    "🔗 Payment Link: {url}": "🔗 To'lov havolasi: {url}",
    "📝 Summary: {text}": "📝 Qisqacha: {text}",
    "+{n} more": "yana {n} ta",
    "no Telegram contact given": "Telegram kontakti berilmagan",
    "🚨 HUMAN SUPPORT NEEDED {tag}": "🚨 ODAM YORDAMI KERAK {tag}",
    "🧾 Order: #{id} (${total}, {status})": "🧾 Buyurtma: #{id} (${total}, {status})",
    "❓ Reason: {reason}": "❓ Sabab: {reason}",
    "💬 Last words: “{text}”": "💬 Oxirgi so'zlari: “{text}”",
    "📝 Call summary: {text}": "📝 Suhbat xulosasi: {text}",
    "💳 PAYMENT CONFIRMED #{id} | ${total} received via {provider}.":
        "💳 TO'LOV TASDIQLANDI #{id} | ${total} {provider} orqali qabul qilindi.",
    "⚠️ PAYMENT REFUSED #{id} | The order had expired and its items have sold since, "
    "so {provider} was told to return the ${total}.":
        "⚠️ TO'LOV RAD ETILDI #{id} | Buyurtma muddati o'tib ketgan va mahsulotlari "
        "sotilib bo'lgan, shuning uchun {provider} ga ${total} ni qaytarish aytildi.",
    "CALL ENDED": "QO'NG'IROQ TUGADI",
    "CALL ENDED, HANDED TO A PERSON": "QO'NG'IROQ TUGADI, ODAMGA O'TKAZILDI",
    "CALL DROPPED, TECHNICAL PROBLEM": "QO'NG'IROQ UZILDI, TEXNIK MUAMMO",
    "CALL ENDED, CALLER WENT SILENT": "QO'NG'IROQ TUGADI, MIJOZ JIM BO'LIB QOLDI",
    "{m} min {s} s": "{m} daq {s} s",
    "{s} s": "{s} s",
    "🛍️ Order #{id} ({status}): {items}": "🛍️ Buyurtma #{id} ({status}): {items}",
    "🛍️ Other orders in this call: {ids}": "🛍️ Shu qo'ng'iroqdagi boshqa buyurtmalar: {ids}",
    "🛍️ No order was placed.": "🛍️ Buyurtma berilmadi.",
    "📝 Transcript attached.": "📝 Suhbat matni faylda.",
    "📝 Transcript:": "📝 Suhbat matni:",
    "Caller": "Mijoz",
    "📞 VOICE CALL SUMMARY ({s}s)": "📞 QO'NG'IROQ XULOSASI ({s} s)",
    "📝 Summary:": "📝 Xulosa:",
    "Not available. The transcript is attached instead.": "Xulosa yo'q. O'rniga suhbat matni ilova qilindi.",
    "📌 Outcome: {outcome}": "📌 Natija: {outcome}",
    "⚠️ The transcript could not be attached.": "⚠️ Suhbat matnini ilova qilib bo'lmadi.",
    # --- Telegram: to the customer ---
    "🛍️ {store}: order #{id}": "🛍️ {store}: buyurtma #{id}",
    "🔗 Pay here: {url}": "🔗 Shu yerda to'lang: {url}",
    "✅ Payment Received! Your order #{id} is confirmed and being prepared for shipment.":
        "✅ To'lov qabul qilindi! #{id} buyurtmangiz tasdiqlandi va jo'natishga tayyorlanmoqda.",
    "Your payment link for order #{id} has expired. Let us know when you're ready to order again!":
        "#{id} buyurtma uchun to'lov havolasining muddati tugadi. Yana buyurtma bermoqchi "
        "bo'lsangiz, bizga yozing!",
    "Your reservation for order #{id} has expired. Let us know when you're ready to order again!":
        "#{id} buyurtma uchun band qilingan mahsulotlar muddati tugadi. Yana buyurtma "
        "bermoqchi bo'lsangiz, bizga yozing!",
    # --- Order states and call outcomes ---
    "pending": "to'lov kutilmoqda",
    "escalated": "odam kutmoqda",
    "paid": "to'langan",
    "cancelled_expired": "muddati o'tgan",
    "Order Placed": "Buyurtma berildi",
    "Escalated": "Odamga o'tkazildi",
    "Inquiry Only": "Faqat so'rov",
    # --- The demo checkout ---
    "Demo checkout. No real payment is taken.": "Demo to'lov sahifasi. Haqiqiy pul olinmaydi.",
    "Payment link not found": "To'lov havolasi topilmadi",
    "Check the link in your Telegram chat.": "Telegram chatingizdagi havolani tekshiring.",
    "Order #{id}": "Buyurtma #{id}",
    "Order #{id} paid": "Buyurtma #{id} to'langan",
    "This order is paid. Thank you!": "Bu buyurtma to'langan. Rahmat!",
    "Order #{id} expired": "Buyurtma #{id} muddati o'tgan",
    "This payment link has expired. Message us to order again.":
        "Bu to'lov havolasining muddati tugagan. Qayta buyurtma berish uchun bizga yozing.",
    "This demo page can't take payments here, because the store has real ones switched on.":
        "Do'konda haqiqiy to'lovlar yoqilgani uchun bu demo sahifa to'lov qabul qila olmaydi.",
    "Pay for order #{id}": "Buyurtma #{id} uchun to'lov",
    "Pay with {provider} (demo)": "{provider} bilan to'lash (demo)",
    "Language": "Til",
}

RUSSIAN = {
    # --- Telegram: to the admin ---
    "🛍️ NEW ORDER CONFIRMED #{id}": "🛍️ НОВЫЙ ЗАКАЗ ПОДТВЕРЖДЁН #{id}",
    "👤 Customer: {contact}": "👤 Клиент: {contact}",
    "📍 Address: {address}": "📍 Адрес: {address}",
    "📦 Items: {items}": "📦 Товары: {items}",
    "💰 Total: ${total} via {provider}": "💰 Итого: ${total} через {provider}",
    "💰 Total: ${total}. No payment link was created.": "💰 Итого: ${total}. Ссылка на оплату не создана.",
    "🔗 Payment Link: {url}": "🔗 Ссылка на оплату: {url}",
    "📝 Summary: {text}": "📝 Кратко: {text}",
    "+{n} more": "и ещё {n}",
    "no Telegram contact given": "контакт в Telegram не указан",
    "🚨 HUMAN SUPPORT NEEDED {tag}": "🚨 НУЖЕН ЧЕЛОВЕК {tag}",
    "🧾 Order: #{id} (${total}, {status})": "🧾 Заказ: #{id} (${total}, {status})",
    "❓ Reason: {reason}": "❓ Причина: {reason}",
    "💬 Last words: “{text}”": "💬 Последние слова: «{text}»",
    "📝 Call summary: {text}": "📝 Итог разговора: {text}",
    "💳 PAYMENT CONFIRMED #{id} | ${total} received via {provider}.":
        "💳 ОПЛАТА ПОДТВЕРЖДЕНА #{id} | ${total} получено через {provider}.",
    "⚠️ PAYMENT REFUSED #{id} | The order had expired and its items have sold since, "
    "so {provider} was told to return the ${total}.":
        "⚠️ ОПЛАТА ОТКЛОНЕНА #{id} | Срок заказа истёк, а его товары уже проданы, "
        "поэтому {provider} попросили вернуть ${total}.",
    "CALL ENDED": "ЗВОНОК ЗАВЕРШЁН",
    "CALL ENDED, HANDED TO A PERSON": "ЗВОНОК ЗАВЕРШЁН, ПЕРЕДАН ЧЕЛОВЕКУ",
    "CALL DROPPED, TECHNICAL PROBLEM": "ЗВОНОК ПРЕРВАН, ТЕХНИЧЕСКАЯ ПРОБЛЕМА",
    "CALL ENDED, CALLER WENT SILENT": "ЗВОНОК ЗАВЕРШЁН, КЛИЕНТ ЗАМОЛЧАЛ",
    "{m} min {s} s": "{m} мин {s} с",
    "{s} s": "{s} с",
    "🛍️ Order #{id} ({status}): {items}": "🛍️ Заказ #{id} ({status}): {items}",
    "🛍️ Other orders in this call: {ids}": "🛍️ Другие заказы в этом звонке: {ids}",
    "🛍️ No order was placed.": "🛍️ Заказ не оформлен.",
    "📝 Transcript attached.": "📝 Расшифровка во вложении.",
    "📝 Transcript:": "📝 Расшифровка:",
    "Caller": "Клиент",
    "📞 VOICE CALL SUMMARY ({s}s)": "📞 ИТОГ ЗВОНКА ({s} с)",
    "📝 Summary:": "📝 Итог:",
    "Not available. The transcript is attached instead.": "Итога нет. Вместо него приложена расшифровка.",
    "📌 Outcome: {outcome}": "📌 Результат: {outcome}",
    "⚠️ The transcript could not be attached.": "⚠️ Не удалось приложить расшифровку.",
    # --- Telegram: to the customer ---
    "🛍️ {store}: order #{id}": "🛍️ {store}: заказ #{id}",
    "🔗 Pay here: {url}": "🔗 Оплатить здесь: {url}",
    "✅ Payment Received! Your order #{id} is confirmed and being prepared for shipment.":
        "✅ Оплата получена! Ваш заказ #{id} подтверждён и готовится к отправке.",
    "Your payment link for order #{id} has expired. Let us know when you're ready to order again!":
        "Срок ссылки на оплату заказа #{id} истёк. Напишите нам, когда захотите заказать снова!",
    "Your reservation for order #{id} has expired. Let us know when you're ready to order again!":
        "Срок брони по заказу #{id} истёк. Напишите нам, когда захотите заказать снова!",
    # --- Order states and call outcomes ---
    "pending": "ждёт оплаты",
    "escalated": "ждёт человека",
    "paid": "оплачен",
    "cancelled_expired": "просрочен",
    "Order Placed": "Заказ оформлен",
    "Escalated": "Передан человеку",
    "Inquiry Only": "Только вопрос",
    # --- The demo checkout ---
    "Demo checkout. No real payment is taken.": "Демо-оплата. Настоящие деньги не списываются.",
    "Payment link not found": "Ссылка на оплату не найдена",
    "Check the link in your Telegram chat.": "Проверьте ссылку в чате Telegram.",
    "Order #{id}": "Заказ #{id}",
    "Order #{id} paid": "Заказ #{id} оплачен",
    "This order is paid. Thank you!": "Этот заказ оплачен. Спасибо!",
    "Order #{id} expired": "Срок заказа #{id} истёк",
    "This payment link has expired. Message us to order again.":
        "Срок этой ссылки истёк. Напишите нам, чтобы заказать снова.",
    "This demo page can't take payments here, because the store has real ones switched on.":
        "Эта демо-страница не принимает оплату: в магазине включены настоящие платежи.",
    "Pay for order #{id}": "Оплата заказа #{id}",
    "Pay with {provider} (demo)": "Оплатить через {provider} (демо)",
    "Language": "Язык",
}

TEXTS: dict[str, dict[str, str]] = {"uz": UZBEK, "ru": RUSSIAN}


def tr(language: str, english: str, **values) -> str:
    """`english` in `language`, with the values filled in."""
    text = TEXTS.get(language, {}).get(english, english)
    return text.format(**values) if values else text
