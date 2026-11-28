CREATE TABLE public.translation_results (
    user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    request_key uuid NOT NULL,
    request_hash text NOT NULL,
    segments jsonb NOT NULL CHECK (jsonb_typeof(segments) = 'array'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, request_key)
);
ALTER TABLE public.translation_results ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.translation_results FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT, INSERT ON public.translation_results TO nene_api;
CREATE POLICY read_own_translation ON public.translation_results FOR SELECT TO nene_api
    USING (user_id::text = current_setting('nene.translation_owner', true));
CREATE POLICY insert_own_translation ON public.translation_results FOR INSERT TO nene_api
    WITH CHECK (user_id::text = current_setting('nene.translation_owner', true));
