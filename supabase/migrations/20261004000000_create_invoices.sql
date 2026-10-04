-- Stores normalized invoice data produced by the PDF extraction pipeline.
-- The API currently writes CSV files; this migration keeps the Supabase schema
-- reproducible when database persistence is enabled.
create table if not exists public.invoices (
  id uuid primary key default gen_random_uuid(),
  sha256 text not null unique,
  row_fingerprint text not null unique,
  source_type text not null default 'pdf' check (source_type in ('pdf', 'csv_import')),
  filename text not null,
  processed_at timestamptz not null default now(),
  page_count integer not null default 0 check (page_count >= 0),
  model text not null,
  needs_review boolean not null default false,
  warnings jsonb not null default '[]'::jsonb,

  language text not null,
  document_type text,
  invoice_number text,
  issue_date date,
  due_date date,

  supplier_name text,
  supplier_tax_id text,
  supplier_address text,
  supplier_email text,
  customer_name text,
  customer_tax_id text,
  customer_address text,
  customer_email text,

  currency text check (currency is null or currency ~ '^[A-Z]{3}$'),
  subtotal numeric,
  discount_total numeric,
  tax_total numeric,
  total numeric,
  amount_due numeric,
  payment_method text,
  iban text,
  purchase_order text,
  notes text,

  items jsonb not null default '[]'::jsonb,
  taxes jsonb not null default '[]'::jsonb,
  extra_fields jsonb not null default '[]'::jsonb,
  raw_invoice jsonb not null,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists invoices_invoice_number_idx
  on public.invoices (invoice_number);

create index if not exists invoices_supplier_tax_id_idx
  on public.invoices (supplier_tax_id);

alter table public.invoices enable row level security;

revoke all on table public.invoices from anon, authenticated;
grant all on table public.invoices to service_role;
