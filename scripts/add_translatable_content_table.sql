-- =========================================================================
-- Add TranslatableContent table for new multilingual system
-- =========================================================================
-- This script adds the translatable_content table that is required by
-- the new TranslatableMixin system in business_app/models/translatable.py
-- =========================================================================

-- Create the translatable_content table
CREATE TABLE IF NOT EXISTS translatable_content (
    id SERIAL PRIMARY KEY,
    
    -- Reference to the original entity
    entity_type VARCHAR(50) NOT NULL,  -- e.g., 'Product', 'ProductCategory'
    entity_id INTEGER NOT NULL,       -- ID of the entity
    field_name VARCHAR(50) NOT NULL,  -- e.g., 'name', 'description'
    
    -- Translation details
    language VARCHAR(5) NOT NULL,     -- e.g., 'en', 'uz', 'ru'
    content TEXT NOT NULL,            -- The translated content
    
    -- Metadata
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    version INTEGER NOT NULL DEFAULT 1,  -- Version for content history
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    -- Unique constraint: one translation per entity+field+language
    UNIQUE(entity_type, entity_id, field_name, language)
);

-- Create indexes for optimal performance
CREATE INDEX IF NOT EXISTS idx_translatable_content_entity_lookup 
    ON translatable_content(entity_type, entity_id);

CREATE INDEX IF NOT EXISTS idx_translatable_content_search 
    ON translatable_content(entity_type, field_name, language);

CREATE INDEX IF NOT EXISTS idx_translatable_content_language 
    ON translatable_content(language);

CREATE INDEX IF NOT EXISTS idx_translatable_content_entity_type 
    ON translatable_content(entity_type);

CREATE INDEX IF NOT EXISTS idx_translatable_content_field_name 
    ON translatable_content(field_name);

CREATE INDEX IF NOT EXISTS idx_translatable_content_active 
    ON translatable_content(is_active) WHERE is_active = true;

-- Create updated_at trigger for the table
CREATE OR REPLACE FUNCTION update_translatable_content_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Drop trigger if it exists and recreate
DROP TRIGGER IF EXISTS trigger_translatable_content_updated_at ON translatable_content;
CREATE TRIGGER trigger_translatable_content_updated_at
    BEFORE UPDATE ON translatable_content
    FOR EACH ROW
    EXECUTE FUNCTION update_translatable_content_updated_at();

-- Add comments for documentation
COMMENT ON TABLE translatable_content IS 'Stores translations for all translatable entity fields across all languages';
COMMENT ON COLUMN translatable_content.entity_type IS 'The model class name (e.g., Product, ProductCategory)';
COMMENT ON COLUMN translatable_content.entity_id IS 'The ID of the entity being translated';
COMMENT ON COLUMN translatable_content.field_name IS 'The field name being translated (e.g., name, description)';
COMMENT ON COLUMN translatable_content.language IS 'Language code (en, uz, ru)';
COMMENT ON COLUMN translatable_content.content IS 'The translated text content';
COMMENT ON COLUMN translatable_content.is_active IS 'Whether this translation is active/visible';
COMMENT ON COLUMN translatable_content.version IS 'Version number for tracking content changes';

-- Verify table was created successfully
SELECT 'translatable_content table created successfully' AS status;