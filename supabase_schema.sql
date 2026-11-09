-- ============================================================
-- 永久组合 · Supabase 表结构
-- 在 Supabase Dashboard → SQL Editor 里整段执行一次即可。
-- 三张表都带 user_id 行级权限（RLS），每个用户只能读写自己的数据。
-- ============================================================

-- 1) 持仓配置（每个用户一行）
create table if not exists public.user_portfolio (
  user_id        uuid primary key references auth.users (id) on delete cascade,
  assets         jsonb not null default '{}'::jsonb,  -- {类别: {target, amount, name, code}}
  monthly_budget numeric not null default 2000,
  age            integer not null default 23,
  updated_at     timestamptz not null default now()
);

-- 2) 交易流水（一笔交易一行）
create table if not exists public.user_trades (
  id          bigserial primary key,
  user_id     uuid not null references auth.users (id) on delete cascade,
  fund_name   text not null default '',
  trade_date  date,
  direction   text not null default '',
  amount      numeric not null default 0,
  category    text not null default '',
  open_logic  text not null default '',
  close_logic text not null default '',
  fee_note    text not null default '',
  created_at  timestamptz not null default now()
);
create index if not exists user_trades_user_date_idx on public.user_trades (user_id, trade_date desc);

-- 3) 每日快照（一天一条，定时任务写入的核心表）
create table if not exists public.user_daily_snapshot (
  user_id       uuid not null references auth.users (id) on delete cascade,
  snapshot_date date not null,
  total_asset   numeric not null default 0,
  daily_return  numeric not null default 0,        -- 组合当日收益率，百分数（0.35 = +0.35%）
  asset_detail  jsonb not null default '{}'::jsonb, -- {类别: {market_value, weight_pct, change_pct}}
  fund_nav      jsonb not null default '{}'::jsonb, -- {基金代码: {name, nav, change_pct}}
  nav_ok        boolean not null default false,
  updated_at    timestamptz not null default now(),
  primary key (user_id, snapshot_date)
);

-- ------------------------------------------------------------
-- 行级权限：只允许 auth.uid() = user_id 的读写
-- ------------------------------------------------------------
alter table public.user_portfolio      enable row level security;
alter table public.user_trades         enable row level security;
alter table public.user_daily_snapshot enable row level security;

drop policy if exists user_portfolio_owner on public.user_portfolio;
create policy user_portfolio_owner on public.user_portfolio
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists user_trades_owner on public.user_trades;
create policy user_trades_owner on public.user_trades
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists user_daily_snapshot_owner on public.user_daily_snapshot;
create policy user_daily_snapshot_owner on public.user_daily_snapshot
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- ------------------------------------------------------------
-- 表级授权：RLS 只负责“行”隔离，PostgREST 还要求角色本身有表权限
-- 缺这段时前端会报 permission denied for table（401 / 42501）
-- ------------------------------------------------------------
grant usage on schema public to anon, authenticated;
grant select, insert, update, delete on public.user_portfolio      to authenticated;
grant select, insert, update, delete on public.user_trades         to authenticated;
grant select, insert, update, delete on public.user_daily_snapshot to authenticated;
grant select, insert, update, delete on public.user_portfolio, public.user_trades, public.user_daily_snapshot to service_role;
grant usage, select on sequence public.user_trades_id_seq to authenticated, service_role;

-- ------------------------------------------------------------
-- 说明
-- 1. 网页端用 anon key + 登录用户身份访问，RLS 自动隔离不同用户。
-- 2. 定时任务（snapshot_job.py --mode cloud）用 service_role key，
--    可跨用户写入，请只配置在服务器 / GitHub Secrets 里，不要放进前端。
-- 3. 如需允许用户删除账号连带清空数据，上面的 on delete cascade 已处理。
-- ------------------------------------------------------------
