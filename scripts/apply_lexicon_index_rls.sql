-- Run in Supabase SQL Editor if the tables already exist and you only need RLS.
-- Safe to re-run: drop policies first, then recreate.

ALTER TABLE public.lexicon_group_index ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lexicon_group_index_documents ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS lexicon_group_index_select_own ON public.lexicon_group_index;
DROP POLICY IF EXISTS lexicon_group_index_insert_own ON public.lexicon_group_index;
DROP POLICY IF EXISTS lexicon_group_index_update_own ON public.lexicon_group_index;
DROP POLICY IF EXISTS lexicon_group_index_delete_own ON public.lexicon_group_index;
DROP POLICY IF EXISTS lexicon_group_index_documents_select_own ON public.lexicon_group_index_documents;
DROP POLICY IF EXISTS lexicon_group_index_documents_insert_own ON public.lexicon_group_index_documents;
DROP POLICY IF EXISTS lexicon_group_index_documents_update_own ON public.lexicon_group_index_documents;
DROP POLICY IF EXISTS lexicon_group_index_documents_delete_own ON public.lexicon_group_index_documents;

CREATE POLICY lexicon_group_index_select_own
ON public.lexicon_group_index
FOR SELECT
TO authenticated
USING (user_id = auth.uid());

CREATE POLICY lexicon_group_index_insert_own
ON public.lexicon_group_index
FOR INSERT
TO authenticated
WITH CHECK (user_id = auth.uid());

CREATE POLICY lexicon_group_index_update_own
ON public.lexicon_group_index
FOR UPDATE
TO authenticated
USING (user_id = auth.uid())
WITH CHECK (user_id = auth.uid());

CREATE POLICY lexicon_group_index_delete_own
ON public.lexicon_group_index
FOR DELETE
TO authenticated
USING (user_id = auth.uid());

CREATE POLICY lexicon_group_index_documents_select_own
ON public.lexicon_group_index_documents
FOR SELECT
TO authenticated
USING (user_id = auth.uid());

CREATE POLICY lexicon_group_index_documents_insert_own
ON public.lexicon_group_index_documents
FOR INSERT
TO authenticated
WITH CHECK (user_id = auth.uid());

CREATE POLICY lexicon_group_index_documents_update_own
ON public.lexicon_group_index_documents
FOR UPDATE
TO authenticated
USING (user_id = auth.uid())
WITH CHECK (user_id = auth.uid());

CREATE POLICY lexicon_group_index_documents_delete_own
ON public.lexicon_group_index_documents
FOR DELETE
TO authenticated
USING (user_id = auth.uid());
