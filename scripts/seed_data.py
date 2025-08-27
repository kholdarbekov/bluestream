#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Comprehensive database seeding script for Blue Stream Water Business Platform
"""
import os
import sys
from datetime import datetime, UTC

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from business_app import create_app, db
from business_app.models.translation import Translation


def seed_essential_translations():
    """Seed essential translations for the multilingual system"""
    print("Seeding essential translations...")
    
    # Essential translations
    ESSENTIAL_TRANSLATIONS = {
        'Home': {'en': 'Home', 'uz': 'Bosh sahifa', 'ru': 'Glavnaya'},
        'Shop': {'en': 'Shop', 'uz': 'Dokon', 'ru': 'Magazin'},
        'Services': {'en': 'Services', 'uz': 'Xizmatlar', 'ru': 'Uslugi'},
        'About Us': {'en': 'About Us', 'uz': 'Biz haqimizda', 'ru': 'O nas'},
        'Contact': {'en': 'Contact', 'uz': 'Aloqa', 'ru': 'Kontakty'},
        'Gallery': {'en': 'Gallery', 'uz': 'Galereya', 'ru': 'Galereya'},
        'Pages': {'en': 'Pages', 'uz': 'Sahifalar', 'ru': 'Stranitsy'},
        'Subscriptions': {'en': 'Subscriptions', 'uz': 'Obunalar', 'ru': 'Podpiski'},
        'Login': {'en': 'Login', 'uz': 'Kirish', 'ru': 'Voyti'},
        'Logout': {'en': 'Logout', 'uz': 'Chiqish', 'ru': 'Vyyti'},
        'Register': {'en': 'Register', 'uz': 'Royxatdan otish', 'ru': 'Registratsiya'},
        'My Account': {'en': 'My Account', 'uz': 'Mening hisobim', 'ru': 'Moy akkaunt'},
        'My Orders': {'en': 'My Orders', 'uz': 'Buyurtmalarim', 'ru': 'Moi zakazy'},
        'Profile Settings': {'en': 'Profile Settings', 'uz': 'Profil sozlamalari', 'ru': 'Nastroyki profilya'},
        'Addresses': {'en': 'Addresses', 'uz': 'Manzillar', 'ru': 'Adresa'},
        'Security': {'en': 'Security', 'uz': 'Xavfsizlik', 'ru': 'Bezopasnost'},
        'Search products...': {'en': 'Search products...', 'uz': 'Qidirish...', 'ru': 'Poisk tovarov...'},
        'Shopping Cart': {'en': 'Shopping Cart', 'uz': 'Savat', 'ru': 'Korzina pokupok'},
        'Add to Cart': {'en': 'Add to Cart', 'uz': 'Savatga qoshish', 'ru': 'Dobavit v korzinu'},
        'Checkout': {'en': 'Checkout', 'uz': 'Tolov', 'ru': 'Oformit zakaz'},
        'Contact Info': {'en': 'Contact Info', 'uz': 'Aloqa malumotlari', 'ru': 'Kontaktnaya informatsiya'},
        'Call Us': {'en': 'Call Us', 'uz': 'Qongirog qiling', 'ru': 'Pozvoните nam'},
        'Address': {'en': 'Address', 'uz': 'Manzil', 'ru': 'Adres'},
        'Useful Links': {'en': 'Useful Links', 'uz': 'Foydali havolalar', 'ru': 'Poleznye ssylki'},
        'Subscribe': {'en': 'Subscribe', 'uz': 'Obuna bolish', 'ru': 'Podpisatsya'},
        'All Rights Reserved': {'en': 'All Rights Reserved', 'uz': 'Barcha huquqlar himoyalangan', 'ru': 'Vse prava zashchishcheny'},
        'Terms of Service': {'en': 'Terms of Service', 'uz': 'Xizmat shartlari', 'ru': 'Usloviya obsluzhivaniya'},
        'Privacy Policy': {'en': 'Privacy Policy', 'uz': 'Maxfiylik siyosati', 'ru': 'Politika konfidentsialnosti'},
        'Save': {'en': 'Save', 'uz': 'Saqlash', 'ru': 'Sohranit'},
        'Cancel': {'en': 'Cancel', 'uz': 'Bekor qilish', 'ru': 'Otmena'},
        'Edit': {'en': 'Edit', 'uz': 'Tahrirlash', 'ru': 'Redaktirovat'},
        'Delete': {'en': 'Delete', 'uz': 'Ochirish', 'ru': 'Udalit'},
        'Submit': {'en': 'Submit', 'uz': 'Jonatish', 'ru': 'Otpravit'},
        'Loading': {'en': 'Loading...', 'uz': 'Yuklanmoqda...', 'ru': 'Zagruzka...'},
        'Logged out successfully': {'en': 'Logged out successfully', 'uz': 'Muvaffaqiyatli chiqildi', 'ru': 'Vyhod vypolnen uspeshno'},
        'Product added to cart': {'en': 'Product added to cart', 'uz': 'Mahsulot qoshildi', 'ru': 'Tovar dobavlen'},
        'Your session has expired. Please login again.': {'en': 'Session expired', 'uz': 'Sessiya tugadi', 'ru': 'Sessiya istekla'},
        'Preloader Close': {'en': 'Close', 'uz': 'Yopish', 'ru': 'Zakryt'}
    }
    
    added_count = 0
    updated_count = 0
    
    for key, languages in ESSENTIAL_TRANSLATIONS.items():
        for lang, value in languages.items():
            try:
                existing = Translation.query.filter_by(key=key, language=lang).first()
                
                if existing:
                    if existing.value != value:
                        existing.value = value
                        existing.updated_at = datetime.now(UTC)
                        updated_count += 1
                        print(f"  Updated: {key} [{lang}]")
                else:
                    new_trans = Translation(
                        key=key,
                        language=lang,
                        value=value,
                        category='essential',
                        is_active=True,
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC)
                    )
                    db.session.add(new_trans)
                    added_count += 1
                    print(f"  Added: {key} [{lang}]")
                    
            except Exception as e:
                print(f"  Error with {key}[{lang}]: {e}")
    
    print(f"Translation seeding complete: {added_count} added, {updated_count} updated")
    return added_count + updated_count > 0


def verify_seeded_data():
    """Verify that seeded data was created correctly"""
    print("\\nVerifying seeded data...")
    
    total_translations = Translation.query.count()
    print(f"Total translations: {total_translations}")
    
    for language in ['en', 'uz', 'ru']:
        count = Translation.query.filter_by(language=language, is_active=True).count()
        print(f"  {language}: {count} translations")
    
    # Test critical translations
    critical_tests = ['Home', 'Shop', 'Login', 'My Account']
    print("\\nTesting critical translations:")
    
    all_good = True
    for key in critical_tests:
        print(f"  {key}:")
        for lang in ['en', 'uz', 'ru']:
            translation = Translation.query.filter_by(key=key, language=lang, is_active=True).first()
            if translation:
                print(f"    {lang}: OK - {translation.value}")
            else:
                print(f"    {lang}: MISSING")
                all_good = False
    
    return all_good


def main():
    """Main seeding function"""
    print("DATABASE SEEDING STARTED")
    print("========================")
    print(f"Timestamp: {datetime.now(UTC).isoformat()}")
    print()
    
    app = create_app()
    
    with app.app_context():
        try:
            # Seed translations
            translations_changed = seed_essential_translations()
            
            # Commit changes
            if translations_changed:
                db.session.commit()
                print("All changes committed to database")
            else:
                print("No changes needed - data already up to date")
            
            # Verify
            verification_passed = verify_seeded_data()
            
            print("\\n========================")
            if verification_passed:
                print("DATABASE SEEDING COMPLETED SUCCESSFULLY!")
                print("\\nWhat was seeded:")
                print("  - Essential UI translations")
                print("  - All 3 languages (en, uz, ru)")
                print("  - Navigation and interface text")
            else:
                print("SEEDING COMPLETED WITH WARNINGS")
                print("Some translations may be missing.")
                
        except Exception as e:
            print(f"\\nSEEDING FAILED: {e}")
            import traceback
            traceback.print_exc()
            db.session.rollback()
            sys.exit(1)


if __name__ == '__main__':
    main()