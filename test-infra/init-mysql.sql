-- MySQL/MariaDB init
CREATE DATABASE IF NOT EXISTS public;
USE public;

CREATE TABLE products (
    id INT PRIMARY KEY,
    name VARCHAR(255),
    category VARCHAR(100),
    price DECIMAL(10,2),
    in_stock TINYINT(1)
);

INSERT INTO products VALUES (1, 'Laptop', 'electronics', 999.99, 1);
INSERT INTO products VALUES (2, 'T-Shirt', 'clothing', 19.99, 1);
INSERT INTO products VALUES (3, 'Headphones', 'electronics', 149.99, 1);
INSERT INTO products VALUES (4, 'Jeans', 'clothing', 49.99, 0);
INSERT INTO products VALUES (5, 'Keyboard', 'electronics', 79.99, 1);
INSERT INTO products VALUES (6, 'Sneakers', 'clothing', 89.99, 1);

-- Metadata-only service account: REFERENCES lets information_schema show columns
-- without granting SELECT (no data access).
CREATE USER 'metadata_user'@'%' IDENTIFIED BY 'metapass';
GRANT REFERENCES ON public.* TO 'metadata_user'@'%';

-- Passthrough user: can SELECT on data tables
CREATE USER 'passthrough_user'@'%' IDENTIFIED BY 'passpass';
GRANT SELECT ON public.* TO 'passthrough_user'@'%';

FLUSH PRIVILEGES;
