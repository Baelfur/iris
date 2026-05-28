-- Oracle init (run as SYSDBA)
ALTER SESSION SET CONTAINER = FREEPDB1;

-- Passthrough user: full data access
CREATE USER public_user IDENTIFIED BY testpass QUOTA UNLIMITED ON USERS;
GRANT CREATE SESSION, CREATE TABLE TO public_user;

CREATE TABLE public_user.products (
    id NUMBER PRIMARY KEY,
    name VARCHAR2(255),
    category VARCHAR2(100),
    price NUMBER(10,2),
    in_stock NUMBER(1)
);

INSERT INTO public_user.products VALUES (1, 'Laptop', 'electronics', 999.99, 1);
INSERT INTO public_user.products VALUES (2, 'T-Shirt', 'clothing', 19.99, 1);
INSERT INTO public_user.products VALUES (3, 'Headphones', 'electronics', 149.99, 1);
INSERT INTO public_user.products VALUES (4, 'Jeans', 'clothing', 49.99, 0);
INSERT INTO public_user.products VALUES (5, 'Keyboard', 'electronics', 79.99, 1);
INSERT INTO public_user.products VALUES (6, 'Sneakers', 'clothing', 89.99, 1);
COMMIT;

-- Metadata-only service account: can see DDL via all_tab_columns, cannot read data.
-- REFERENCES on the table makes columns visible in all_tab_columns without granting SELECT.
CREATE USER metadata_user IDENTIFIED BY metapass;
GRANT CREATE SESSION TO metadata_user;
GRANT REFERENCES ON public_user.products TO metadata_user;
