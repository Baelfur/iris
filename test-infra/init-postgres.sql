-- PostgreSQL init
CREATE SCHEMA IF NOT EXISTS public;

CREATE TABLE public.products (
    id INT PRIMARY KEY,
    name VARCHAR(255),
    category VARCHAR(100),
    price DECIMAL(10,2),
    in_stock INT
);

INSERT INTO public.products VALUES (1, 'Laptop', 'electronics', 999.99, 1);
INSERT INTO public.products VALUES (2, 'T-Shirt', 'clothing', 19.99, 1);
INSERT INTO public.products VALUES (3, 'Headphones', 'electronics', 149.99, 1);
INSERT INTO public.products VALUES (4, 'Jeans', 'clothing', 49.99, 0);
INSERT INTO public.products VALUES (5, 'Keyboard', 'electronics', 79.99, 1);
INSERT INTO public.products VALUES (6, 'Sneakers', 'clothing', 89.99, 1);

-- Metadata-only service account: can see columns in information_schema but not read data.
-- REFERENCES makes columns visible in information_schema.columns without granting SELECT.
CREATE USER metadata_user WITH PASSWORD 'metapass';
GRANT USAGE ON SCHEMA public TO metadata_user;
GRANT REFERENCES ON ALL TABLES IN SCHEMA public TO metadata_user;

-- Passthrough user: can SELECT on data tables
CREATE USER passthrough_user WITH PASSWORD 'passpass';
GRANT USAGE ON SCHEMA public TO passthrough_user;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO passthrough_user;
