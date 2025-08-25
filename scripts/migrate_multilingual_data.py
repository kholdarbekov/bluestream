#!/usr/bin/env python3
"""
Migration script to move existing multilingual data to the new translatable content system
This script migrates data from language-specific columns (name_en, name_ru, etc.) 
to the new TranslatableContent table.

UPDATED: December 2024 - Compatible with current system architecture
- Migrates ProductCategory legacy columns: name_ru, name_en, description_ru, description_en
- Sets up baseline translations for all @translatable decorated models
- Supports dry-run mode for safety
"""
import os
import sys
import argparse
from datetime import datetime, UTC

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from business_app import create_app, db
from business_app.models.translatable import TranslatableContent
from business_app.models.product import Product, ProductCategory
from business_app.models.subscription import SubscriptionPlan
from business_app.models.notification import NotificationTemplate
from business_app.models.review import Review
from business_app.models.loyalty import LoyaltyProgram, LoyaltyTier
from business_app.models.payment import PaymentMethod
from business_app.models.delivery import DeliveryZone


def migrate_product_categories():
    """Migrate ProductCategory multilingual data"""
    print("Migrating ProductCategory multilingual data...")
    
    categories = ProductCategory.query.all()
    migrated_count = 0
    
    for category in categories:
        translations = {}
        
        # Prepare translations dict
        if category.name:
            translations.setdefault('name', {})['uz'] = category.name
        if category.name_en:
            translations.setdefault('name', {})['en'] = category.name_en
        if category.name_ru:
            translations.setdefault('name', {})['ru'] = category.name_ru
        
        if category.description:
            translations.setdefault('description', {})['uz'] = category.description
        if category.description_en:
            translations.setdefault('description', {})['en'] = category.description_en
        if category.description_ru:
            translations.setdefault('description', {})['ru'] = category.description_ru
        
        # Migrate to translatable content
        if translations:
            try:
                category.set_translations(translations)
                migrated_count += 1
                print(f"  Migrated category {category.id}: {category.name}")
            except Exception as e:
                print(f"  Error migrating category {category.id}: {e}")
    
    print(f"Migrated {migrated_count} categories")
    return migrated_count


def migrate_products():
    """Migrate Product multilingual data (if any language-specific columns exist)"""
    print("Migrating Product multilingual data...")
    
    # Note: Current Product model doesn't have language-specific columns like name_en, name_ru
    # But this is where we would migrate them if they existed
    
    products = Product.query.all()
    migrated_count = 0
    
    for product in products:
        # For now, just ensure the default Uzbek content is available in translatable format
        # This helps establish the baseline for future translations
        translations = {}
        
        if product.name:
            translations.setdefault('name', {})['uz'] = product.name
        if product.description:
            translations.setdefault('description', {})['uz'] = product.description
        if product.short_description:
            translations.setdefault('short_description', {})['uz'] = product.short_description
        if product.ingredients:
            translations.setdefault('ingredients', {})['uz'] = product.ingredients
        if product.meta_title:
            translations.setdefault('meta_title', {})['uz'] = product.meta_title
        if product.meta_description:
            translations.setdefault('meta_description', {})['uz'] = product.meta_description
        
        # Migrate to translatable content
        if translations:
            try:
                product.set_translations(translations)
                migrated_count += 1
                print(f"  Set up translatable baseline for product {product.id}: {product.name}")
            except Exception as e:
                print(f"  Error setting up product {product.id}: {e}")
    
    print(f"Set up translatable baseline for {migrated_count} products")
    return migrated_count


def migrate_all_translatable_models():
    """Set up baseline translations for all @translatable decorated models"""
    print("Setting up baseline translations for all translatable models...")
    
    translatable_models = [
        (SubscriptionPlan, ['name', 'description']),
        (NotificationTemplate, ['subject', 'body']),
        (Review, ['comment']),
        (LoyaltyProgram, ['name', 'description']),
        (LoyaltyTier, ['name', 'description']),
        (PaymentMethod, ['display_name', 'description']),
        (DeliveryZone, ['name', 'description'])
    ]
    
    total_migrated = 0
    
    for model_class, fields in translatable_models:
        print(f"\n  Processing {model_class.__name__}...")
        model_migrated = 0
        
        try:
            entities = model_class.query.all()
            
            for entity in entities:
                translations = {}
                
                # Set up baseline Uzbek translations for each translatable field
                for field_name in fields:
                    if field_name in entity._translatable_fields:
                        field_value = getattr(entity, field_name, None)
                        if field_value:
                            translations.setdefault(field_name, {})['uz'] = field_value
                
                # Migrate to translatable content
                if translations:
                    try:
                        entity.set_translations(translations)
                        model_migrated += 1
                        print(f"    Set up {entity.id}: {getattr(entity, fields[0], 'N/A')[:50]}...")
                    except Exception as e:
                        print(f"    Error setting up {entity.id}: {e}")
            
            print(f"  Set up {model_migrated} {model_class.__name__} entities")
            total_migrated += model_migrated
            
        except Exception as e:
            print(f"  Error processing {model_class.__name__}: {e}")
    
    print(f"\nTotal entities with baseline translations: {total_migrated}")
    return total_migrated


def verify_migration():
    """Verify the migration was successful"""
    print("\nVerifying migration...")
    
    # Check translatable content count
    content_count = TranslatableContent.query.count()
    print(f"Total translatable content records: {content_count}")
    
    # Check categories
    categories_with_translations = TranslatableContent.query.filter_by(
        entity_type='ProductCategory'
    ).distinct(TranslatableContent.entity_id).count()
    print(f"Categories with translations: {categories_with_translations}")
    
    # Check products
    products_with_translations = TranslatableContent.query.filter_by(
        entity_type='Product'
    ).distinct(TranslatableContent.entity_id).count()
    print(f"Products with translations: {products_with_translations}")
    
    # Show sample translations
    print("\nSample translations:")
    samples = TranslatableContent.query.limit(5).all()
    for sample in samples:
        print(f"  {sample.entity_type}:{sample.entity_id}.{sample.field_name}[{sample.language}] = '{sample.content[:50]}...'")
    
    return True


def clean_legacy_columns():
    """Clean up legacy language columns (DANGEROUS - use with caution)"""
    print("\n⚠️  WARNING: This will remove legacy language columns!")
    print("Make sure you have backed up your database before proceeding.")
    
    response = input("Are you sure you want to remove legacy columns? (type 'YES' to confirm): ")
    if response != 'YES':
        print("Skipping cleanup of legacy columns.")
        return False
    
    try:
        # Remove legacy columns from ProductCategory
        # Note: This would require SQLAlchemy DDL operations or manual SQL
        # For safety, we'll just print what would be done
        print("Would remove these legacy columns from ProductCategory:")
        print("  - name_ru")
        print("  - name_en") 
        print("  - description_ru")
        print("  - description_en")
        
        # In a real migration, you would use:
        # ALTER TABLE product_categories DROP COLUMN name_ru;
        # ALTER TABLE product_categories DROP COLUMN name_en;
        # etc.
        
        print("⚠️  Legacy column cleanup not implemented - requires manual SQL DDL operations")
        return False
        
    except Exception as e:
        print(f"Error cleaning legacy columns: {e}")
        return False


def main():
    """Main migration function"""
    parser = argparse.ArgumentParser(description='Migrate multilingual data to translatable content system')
    parser.add_argument('--verify-only', action='store_true', help='Only verify existing migration')
    parser.add_argument('--clean-legacy', action='store_true', help='Clean up legacy columns (DANGEROUS)')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be migrated without making changes')
    
    args = parser.parse_args()
    
    # Create Flask app context
    app = create_app()
    
    with app.app_context():
        if args.verify_only:
            verify_migration()
            return
        
        if args.dry_run:
            print("🔍 DRY RUN MODE - No changes will be made\n")
        
        print("🚀 Starting multilingual data migration...")
        print(f"Timestamp: {datetime.now(UTC).isoformat()}")
        print("-" * 50)
        
        total_migrated = 0
        
        try:
            # Migrate categories
            if not args.dry_run:
                migrated = migrate_product_categories()
                total_migrated += migrated
            else:
                categories = ProductCategory.query.count()
                print(f"Would migrate {categories} product categories")
            
            # Migrate products
            if not args.dry_run:
                migrated = migrate_products()
                total_migrated += migrated
            else:
                products = Product.query.count()
                print(f"Would set up translatable baseline for {products} products")
            
            # Migrate all other translatable models
            if not args.dry_run:
                migrated = migrate_all_translatable_models()
                total_migrated += migrated
            else:
                print("Would set up baseline translations for all other translatable models")
            
            # Commit changes
            if not args.dry_run:
                db.session.commit()
                print(f"\n✅ Migration completed successfully!")
                print(f"Total entities migrated: {total_migrated}")
                
                # Verify migration
                verify_migration()
            else:
                print(f"\n🔍 DRY RUN completed - no changes made")
            
            # Optional cleanup
            if args.clean_legacy and not args.dry_run:
                clean_legacy_columns()
                
        except Exception as e:
            print(f"\n❌ Migration failed: {e}")
            if not args.dry_run:
                db.session.rollback()
            sys.exit(1)


if __name__ == '__main__':
    main()