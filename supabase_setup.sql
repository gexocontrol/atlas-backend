CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS public.users (
    id            UUID         DEFAULT gen_random_uuid() PRIMARY KEY,
    email         TEXT         NOT NULL UNIQUE,
    name          TEXT         NOT NULL,
    role          TEXT         NOT NULL DEFAULT 'student'
                               CHECK (role IN ('student', 'teacher', 'school')),
    password_hash TEXT         NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION public.handle_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS users_updated_at ON public.users;
CREATE TRIGGER users_updated_at
    BEFORE UPDATE ON public.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_updated_at();

CREATE INDEX IF NOT EXISTS idx_users_email ON public.users (email);

CREATE TABLE IF NOT EXISTS public.exam_results (
    id          UUID         DEFAULT gen_random_uuid() PRIMARY KEY,
    user_email  TEXT         NOT NULL REFERENCES public.users (email) ON DELETE CASCADE,
    exam_type   TEXT         NOT NULL,
    score       INTEGER      CHECK (score BETWEEN 0 AND 100),
    feedback    TEXT,
    subject     TEXT,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_exam_results_user_email ON public.exam_results (user_email);
CREATE INDEX IF NOT EXISTS idx_exam_results_created_at ON public.exam_results (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_exam_results_type ON public.exam_results (exam_type);

ALTER TABLE public.users        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exam_results ENABLE ROW LEVEL SECURITY;

CREATE POLICY "users_select_own"
    ON public.users FOR SELECT
    USING (email = auth.jwt() ->> 'email');

CREATE POLICY "users_insert_service_only"
    ON public.users FOR INSERT
    WITH CHECK (false);

CREATE POLICY "users_update_service_only"
    ON public.users FOR UPDATE
    USING (false);

CREATE POLICY "users_delete_service_only"
    ON public.users FOR DELETE
    USING (false);

CREATE POLICY "exam_results_select_own"
    ON public.exam_results FOR SELECT
    USING (user_email = auth.jwt() ->> 'email');

CREATE POLICY "exam_results_insert_service_only"
    ON public.exam_results FOR INSERT
    WITH CHECK (false);

CREATE POLICY "exam_results_update_service_only"
    ON public.exam_results FOR UPDATE
    USING (false);

CREATE POLICY "exam_results_delete_service_only"
    ON public.exam_results FOR DELETE
    USING (false);
