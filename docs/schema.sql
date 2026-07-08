-- =============================================================================
-- Supabase schema snapshot
-- Project: limkntwcgezlqvorxtlg
-- Snapshot date: 2026-07-04
--
-- Reference only — not auto-applied. Re-run against a fresh Supabase project
-- to recreate the full schema from scratch. Update this file whenever you add
-- or alter tables/columns via the Supabase dashboard.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

CREATE TABLE public.tutors (
  email text PRIMARY KEY
);

CREATE TABLE public.students (
  id                 uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  name               text        NOT NULL,
  contact_person     text,
  contact_phone      text,
  student_phone      text,
  mode               text        NOT NULL DEFAULT 'My Python Syllabus',
  class_schedule     jsonb       NOT NULL DEFAULT '[]',
  fee_per_hour       numeric     NOT NULL DEFAULT 60,
  payment_method     text        NOT NULL DEFAULT 'Monthly',
  latest_payment     text,
  today_homework     text,
  notes              text,
  status             text        NOT NULL DEFAULT 'Active',
  google_meet_link   text,
  google_drive_link  text,
  access_emails      text[]      DEFAULT '{}',
  calendar_event_ids text[],
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.templates (
  id         text        PRIMARY KEY,
  content    text        NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.settings (
  key   text PRIMARY KEY,
  value text NOT NULL
);

CREATE TABLE public.agent_conversations (
  id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  lg_contents      jsonb,
  prev_lg_contents jsonb,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.agent_messages (
  id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid        NOT NULL REFERENCES public.agent_conversations(id) ON DELETE CASCADE,
  role            text        NOT NULL CHECK (role = ANY (ARRAY['user', 'agent'])),
  content         text        NOT NULL DEFAULT '',
  steps           jsonb       NOT NULL DEFAULT '[]',
  is_error        boolean     NOT NULL DEFAULT false,
  students        jsonb,
  schedule_students jsonb,
  slot_data       jsonb,
  created_at      timestamptz NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- Functions
-- ---------------------------------------------------------------------------

-- Returns true if the calling user's email is in the tutors table.
-- Used in RLS policies and proxy.ts route protection.
CREATE OR REPLACE FUNCTION public.is_tutor()
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
AS $$
  SELECT EXISTS (SELECT 1 FROM tutors WHERE email = auth.email());
$$;

-- Returns true if the given email belongs to a tutor.
-- Called by the admin login page before sending a magic link.
CREATE OR REPLACE FUNCTION public.check_tutor_access(p_email text)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
AS $$
  SELECT EXISTS (SELECT 1 FROM tutors WHERE email = p_email);
$$;

-- Returns true if the given email appears in any student's access_emails array.
-- Called by the student portal login page before sending a magic link.
CREATE OR REPLACE FUNCTION public.check_portal_access(p_email text)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
AS $$
  SELECT EXISTS (
    SELECT 1 FROM students WHERE p_email = ANY(access_emails)
  );
$$;

-- Trigger function: sets updated_at = now() on students UPDATE.
CREATE OR REPLACE FUNCTION public.update_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

-- Trigger function: sets updated_at = now() on templates UPDATE.
CREATE OR REPLACE FUNCTION public.update_templates_updated_at()
RETURNS trigger
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;


-- ---------------------------------------------------------------------------
-- Triggers
-- ---------------------------------------------------------------------------

CREATE TRIGGER students_updated_at
  BEFORE UPDATE ON public.students
  FOR EACH ROW EXECUTE FUNCTION public.update_updated_at();

CREATE TRIGGER templates_updated_at
  BEFORE UPDATE ON public.templates
  FOR EACH ROW EXECUTE FUNCTION public.update_templates_updated_at();


-- ---------------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------------

ALTER TABLE public.tutors             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.students           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.templates          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.settings           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_messages     ENABLE ROW LEVEL SECURITY;

-- tutors: no explicit policy — only accessible via SECURITY DEFINER functions
-- (is_tutor, check_tutor_access) which bypass RLS using the service role.

-- students
CREATE POLICY admin_full_access ON public.students
  FOR ALL
  USING (is_tutor())
  WITH CHECK (is_tutor());

CREATE POLICY student_read_own ON public.students
  FOR SELECT
  USING (auth.email() = ANY (access_emails));

CREATE POLICY "Authenticated users only" ON public.students
  FOR ALL
  USING (auth.role() = 'authenticated');

-- templates
CREATE POLICY "Authenticated users only" ON public.templates
  FOR ALL
  USING (auth.role() = 'authenticated');

-- settings
CREATE POLICY tutor_full_access ON public.settings
  FOR ALL
  USING (is_tutor())
  WITH CHECK (is_tutor());

-- agent_conversations
CREATE POLICY tutor_full_access ON public.agent_conversations
  FOR ALL
  USING (is_tutor());

-- agent_messages
CREATE POLICY tutor_full_access ON public.agent_messages
  FOR ALL
  USING (is_tutor());


-- ---------------------------------------------------------------------------
-- Seed data
-- ---------------------------------------------------------------------------

-- Add your tutor email (replace with your actual email)
INSERT INTO public.tutors (email) VALUES ('your-email@example.com');

-- Default message templates (sanitized — replace [Name], [Student], and URLs with real values)
INSERT INTO public.templates (id, content) VALUES
  ('payment',
   'Hi [Name], just a gentle reminder regarding the tuition fee. There are 5 sessions in July (1st, 8th, 15th, 22nd, and 29th), bringing the total to RM300. Thank you 😄'),
  ('payment2',
   'Hi [Name], just a gentle reminder regarding the tuition fee. There are 5 sessions in July (1st, 8th, 15th, 22nd, and 29th). With 2 sessions carried over from the previous classes, bringing the total to RM300. Thank you. 😄'),
  ('review_request1',
   'Hi [Name]. It''s been a while since we started the classes. Hope you''ve been enjoying them so far. If possible, could you kindly leave me a 5-star review with a simple description on the Superprof platform? It would mean a lot and really help me out. Thank you so much 😄'),
  ('review_request2',
   'Hi [Name]. It''s been a while since [Student] and I started the classes. Hope she has been enjoying them so far. If possible, could you kindly leave me a 5-star review with a simple description on the Superprof platform? It would mean a lot and really help me out. Thank you so much 😄'),
  ('recommendation_request1',
   'Hi [Name]. Hope you''ve been enjoying the classes so far. If possible, I''d also really appreciate it if u could leave a recommendation for me on Superprof platform. It would help me a lot. Thank you so much 😄

👉 https://www.superprof.com.my/your-profile-link'),
  ('recommendation_request2',
   'Hi [Name]. Just wanted to let you know that I''ve completed the whole syllabus with [Student]. Hope she has been enjoying the classes so far. If possible, I''d also really appreciate it if you could leave a recommendation for me on Superprof platform. It would help me a lot. Thank you so much 😄

👉 https://www.superprof.com.my/your-profile-link'),
  ('first_approach',
   'Hi [Name], this is [Your Name] from Superprof. 😄

I saw your request about finding a programming tutor. You can also check out my self-hosted website for more details: https://your-website.vercel.app

Feel free to share more your learning needs as well. Looking forward to hear from you. 🤞')
ON CONFLICT (id) DO NOTHING;

-- Settings keys used by the app (values filled in via Google OAuth flow)
INSERT INTO public.settings (key, value) VALUES
  ('google_refresh_token', ''),
  ('google_access_token',  ''),
  ('timetable_rules',      '')
ON CONFLICT (key) DO NOTHING;
