"""Website redesign (2026-11-14): delete dead and false translation rows, print what was there first, drop the cache.
Run on PRODUCTION once, AFTER every seed of the redesign deploy:
    docker compose exec -T business_app python - < scripts/cleanup_web_redesign_translation_rows.py
Keep the PRE lines it prints: they are the only record of the deleted values.
Keys (69): the loyalty tiers' invented perks (SITE-01), the 21 natural-English /subscriptions rows (subs-r07), the
home page's dead Aqua Club/CTA rows (home-10), 30 DB-only and 6 seeded cabinet rows no template reads (SITE-24),
web.account.date_full (SITE-07), landing.custom_subscription.review (read by nothing), and two labels the site no longer
prints (SITE-16). Every key was grepped across templates, JS, Python, both bots, admin_ui and tests before deletion."""
import json
from business_app import create_app, db
from business_app.models.translation import Translation
from business_app.utils.translations import translation_service
KEYS = [
 "loyalty_guide.tier.1.benefit1",
 "loyalty_guide.tier.2.benefit1",
 "loyalty_guide.tier.3.benefit1",
 "Never Run Out",
 "Your subscription places each order for you on schedule, so you never forget to order water and never run out when you need it most.",
 "Delivery is always free, on subscription deliveries as on every order.",
 "Earn AquaCoins on every subscription delivery at your tier's rate, and redeem them for rewards.",
 "What do I get with a subscription?",
 "Your subscription places each order for you on schedule, so you never forget to order water and never run out when you need it most. Delivery is always free, and you earn AquaCoins on every subscription delivery at your tier's rate. Subscription deliveries are billed at the regular price.",
 "Auto",
 "Orders placed for you",
 "Delivery on every order",
 "Let your water orders take care of themselves, and never run out.",
 "Choose a delivery frequency below to preview your schedule",
 "Your Delivery Schedule",
 "Build your subscription with the subscription builder on our home page, or start one in our Telegram bot. Pick your products and schedule, and we place every order for you.",
 "Open the Subscription Builder",
 "Subscribe in Telegram",
 "Create your subscription on our website or in our Telegram bot",
 "Absolutely! Unlike fixed plans, our subscriptions are fully customizable. Choose any products from our catalog, set your own quantities, select your preferred delivery frequency, and even set specific delivery days. Change items, quantities, or the schedule anytime in your online account or in our Telegram bot. New items and quantities apply from the next order we place, and a new frequency, day, or time slot sets your next delivery by the new schedule. An order we have already placed is not changed.",
 "You can pause your subscription whenever you like, for example during a vacation, and resume it when you are back, in your online account or in our Telegram bot. You can also skip any upcoming delivery in your online account or in our Telegram bot. Pausing, skipping, or cancelling cancels an upcoming order that is not yet paid. An order you have already paid for, or one already picked up for delivery, cannot be skipped or paused: it stays and is delivered, and the change applies from the following delivery.",
 "A subscription can be paid by card through Click, in cash on delivery, or, for business customers, through a business account. Each scheduled delivery is placed as a regular order and paid with the payment method on your subscription, which you can change in your online account or in our Telegram bot.",
 "Your subscription delivers to the address you chose. You can change it in your online account or in our Telegram bot by picking one of your saved addresses. Your upcoming deliveries move to the new address too, unless the driver already has them.",
 "Each scheduled delivery is placed as a regular order, so it follows the same payment steps as any order. If a Click payment does not go through, you can pay the order again in your online account or in our Telegram bot until its pay-by time. If you need help, message us in our Telegram bot.",
 "cabinet.subs.cancel.coins",
 "cabinet.subs.cancel.keep",
 "cabinet.subs.cancel.kept_awaiting_date",
 "cabinet.subs.cancel.kept_other",
 "cabinet.subs.cancel.no_more",
 "cabinet.subs.cancel.will_cancel",
 "cabinet.subs.footer.cancel",
 "cabinet.subs.hero.kept_on_the_way",
 "cabinet.subs.hero.kept_order",
 "cabinet.subs.hero.pause",
 "cabinet.subs.hero.pay",
 "cabinet.subs.hero.resume",
 "cabinet.subs.hero.skip",
 "cabinet.subs.hero.time_tbc",
 "cabinet.subs.pause.do",
 "cabinet.subs.pause.keep",
 "cabinet.subs.pause.text",
 "cabinet.subs.payment.not_offered",
 "cabinet.subs.schedule.no_slots",
 "cabinet.subs.schedule.slot_pick",
 "cabinet.subs.skip.do",
 "cabinet.subs.skip.keep",
 "cabinet.subs.skip.keep_both",
 "cabinet.subs.skip.reason.away",
 "cabinet.subs.skip.reason.other",
 "cabinet.subs.skip.reason.still_have_water",
 "cabinet.subs.skip.text_on_the_way",
 "cabinet.subs.skip.text_paid",
 "cabinet.subs.skip.text_plain",
 "cabinet.subs.skip.text_unpaid",
 "cabinet.account.recent_orders",
 "cabinet.account.view",
 "cabinet.addresses.delete",
 "cabinet.addresses.full_address",
 "cabinet.loyalty.earn_more_to_keep",
 "cabinet.orders.view_details",
 "landing.loyalty.public_title",
 "landing.loyalty.public_subtitle",
 "landing.loyalty.public_earn_title",
 "landing.loyalty.public_tiers_title",
 "landing.cta.description",
 "web.account.date_full",
 "landing.custom_subscription.review",
 "web.shell.footer.products",
 "web.cabinet.orders.new"
]
app = create_app()
with app.app_context():
    rows = Translation.query.filter(Translation.key.in_(KEYS)).order_by(Translation.key, Translation.language).all()
    for r in rows:
        print("PRE\t%s\t%s\t%s\t%s" % (r.key, r.language, r.category, json.dumps(r.value, ensure_ascii=False)))
    n = Translation.query.filter(Translation.key.in_(KEYS)).delete(synchronize_session=False)
    db.session.commit()
    translation_service.clear_cache()
    print("DELETED", n)
