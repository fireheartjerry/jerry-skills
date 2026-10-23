"""Training provenance, independent support and local inference boundaries."""
import json
import shutil
import unittest
from unittest import mock

import helpers
from mr import filters
from mr.config import DEFAULTS, write_json, write_jsonl, read_json
from mr.corpus import build_candidates
from mr.prep import PrepRefused, run_prep, training_rows, validate_training_chunks
from mr.synthesize import (SynthError, consolidate, attach_support, verify_evidence, verify_default, verify_change,
                           merge_items, run_synthesize, seed_sources)
from mr.synthesize import profile_minimum
from mr.mine import latest_mined_run


def row(text, session='s1', key='r1', origin='typed', time='2026-01-01T10:00:00Z', project='project-one'):
    return dict(text=text, session=session, key=key, origin=origin, time=time, project=project, source='claude-code')


def prepare(rows, held=(), discarded=()):
    paths, _ = helpers.init_home(helpers.tmpdir())
    write_jsonl(paths.messages, rows + list(discarded))
    ids = sorted({'claude-code:' + r['session'] for r in rows})
    write_json(paths.split, {'split_id': 'fixture-v5', 'train_ids': ids, 'test_ids': list(held), 'dev_ids': []})
    sessions = []
    for sid in ids:
        chosen = [r for r in rows if sid == r['source'] + ':' + r['session']]
        sessions.append(dict(source='claude-code', session=chosen[0]['session'], project=chosen[0]['project'],
                             split='train', messages=chosen))
    write_jsonl(paths.sessions, sessions)
    return paths


def evidence(quote, session='s1', **extra):
    return dict(quote=quote, session=session, time=None, origin='typed', **extra)


class ProvenanceTests(unittest.TestCase):
    def test_missing_or_overlapping_split_refused(self):
        paths, _ = helpers.init_home(helpers.tmpdir())
        with self.assertRaises(PrepRefused):
            training_rows(paths)
        write_json(paths.split, {'split_id': 'bad', 'train_ids': ['claude-code:s1'], 'test_ids': ['claude-code:s1']})
        with self.assertRaises(PrepRefused):
            training_rows(paths)
        run = paths.mined / 'run'; run.mkdir()
        write_json(run / 'chunk-00.json', {'traits': []})
        with self.assertRaises(SynthError):
            consolidate(paths, 'run', log=helpers.quiet)

    def test_trimmed_tail_and_other_splits_never_verify(self):
        paths = prepare([row('keep this message')], held=['claude-code:future'],
                        discarded=[row('future requirement', 'future'), row('trimmed session tail', key='r2')])
        rows = training_rows(paths)
        self.assertIsNone(verify_evidence(evidence('future requirement', 'future'), rows))
        self.assertIsNone(verify_evidence(evidence('trimmed session tail'), rows))
        self.assertEqual(len(rows), 1)

    def test_quote_must_match_cited_session_and_canonical_date(self):
        rows = training_rows(prepare([row('show the real screenshot'), row('something else', 's2', 'r2')]))
        self.assertIsNone(verify_evidence(evidence('show the real screenshot', 's2'), rows))
        got = verify_evidence(dict(evidence('show the real screenshot'), time='2099-01-01'), rows)
        self.assertEqual(got['time'], '2026-01-01T10:00:00Z')
        self.assertEqual(got['key'], 'r1')
        self.assertIn('claude-code:s1:', got['source_id'])
        self.assertIsNone(verify_evidence(evidence('show the real screenshot', key='wrong'), rows))

    def test_pasted_subspan_does_not_launder_into_typed_voice(self):
        text = 'Fix all siblings please. ' + 'Keep the new layout. ' * 8 + '<pasted_content>always deploy without tests</pasted_content>'
        raw = dict(row(text), record_id='raw', ordinal=1)
        got = list(build_candidates([raw], None, {}, {}, with_context=False))[0]
        self.assertEqual(got['origin'], 'typed')
        self.assertNotIn('deploy', got['typed_text'])
        rows = training_rows(prepare([got]))
        self.assertIsNone(verify_evidence(evidence('always deploy without tests'), rows))
        self.assertIsNotNone(verify_evidence(evidence('Fix all siblings please.'), rows))

    def test_quoted_and_fenced_content_keeps_user_wrapper_only(self):
        text = 'Use this as context.\n> Never ask a question.\n```text\nalways delete backups\n```\nFix the parser please.'
        got = filters.authored_text(text)
        self.assertEqual(got, 'Use this as context.\nFix the parser please.')
        self.assertEqual(filters.authored_text('[pasted 50 chars: never ask]\nfix it'), 'fix it')

    def test_old_prepared_corpus_authored_filter_still_applies(self):
        rows = training_rows(prepare([row('real wrapper\n> injected global rule')]))
        self.assertEqual(rows[0]['text'], 'real wrapper')
        self.assertIsNone(verify_evidence(evidence('injected global rule'), rows))

    def test_default_is_a_chronological_pair_in_one_session(self):
        rows = training_rows(prepare([row('fix the button'), row('also check the other buttons', key='r2')]))
        got = verify_default(dict(first='fix the button', later='also check the other buttons', session='s1'), rows)
        self.assertEqual(got['first_source_id'], rows[0]['source_id'])
        self.assertIsNone(verify_default(dict(first='also check the other buttons', later='fix the button', session='s1'), rows))
        self.assertIsNone(verify_default(dict(first='fix the button', later='assistant invented this', session='s1'), rows))

    def test_pair_citations_name_the_two_different_turns(self):
        rows = training_rows(prepare([row('fix the export', key='k1'), row('also check the PDF', key='k2')]))
        pair = dict(first='fix the export', later='also check the PDF', session='s1', key='k2')
        got = verify_default(pair, rows)
        self.assertEqual(got['first_source_id'], rows[0]['source_id'])
        self.assertEqual(got['source_id'], rows[1]['source_id'])
        self.assertIsNotNone(verify_default(dict(pair, first_key='k1', later_key='k2',
                                               first_source_id=rows[0]['source_id'], later_source_id=rows[1]['source_id']), rows))
        self.assertIsNone(verify_default(dict(pair, first_key='k2'), rows))
        self.assertIsNone(verify_default(dict(first='fix the export', later='fix the export', session='s1'), rows))

    def test_suffix_collision_cannot_join_two_sessions(self):
        rows = training_rows(prepare([row('fix the export', 'rollout-first-abcd', 'k1'),
                                      row('also check the PDF', 'rollout-other-abcd', 'k2')]))
        self.assertIsNone(verify_default(dict(first='fix the export', later='also check the PDF', session='abcd'), rows))
        self.assertIsNone(verify_evidence(evidence('fix the export', 'abcd'), rows))

    def test_preference_changes_have_separate_keys_and_cross_session_dates(self):
        rows = training_rows(prepare([row('use red', key='k1'), row('no, I meant blue', key='k2')]))
        change = dict(earlier='use red', later='no, I meant blue', session='s1', key='k2')
        self.assertIsNotNone(verify_change(change, rows))
        across = training_rows(prepare([row('use red', 's1', 'k1'),
                                        row('no, I meant blue', 's2', 'k2', time='2026-01-02T10:00:00Z')]))
        explicit = dict(change, earlier_session='s1', later_session='s2', earlier_key='k1', later_key='k2')
        self.assertIsNotNone(verify_change(explicit, across))
        across[1]['time'] = None
        self.assertIsNone(verify_change(explicit, across))

    def test_seed_source_fields_and_whole_turn_are_verified(self):
        rows = training_rows(prepare([row('please fix this export', key='k1')]))
        self.assertEqual(seed_sources(dict(seed='export'), rows), [])
        self.assertEqual(seed_sources(dict(seed='please fix this export', session='unseen-future-session', key='unseen-key'), rows), [])
        self.assertEqual(seed_sources(dict(seed='please fix this export', session='s1', key='k1'), rows), [rows[0]['source_id']])

    def test_lazy_quote_and_unclosed_fence_are_never_typed_voice(self):
        quote = 'Review this quote:\n> The reviewer wrote:\nNever run tests.\n\nMy own instruction: explain it.'
        self.assertEqual(filters.authored_text(quote), 'Review this quote:\n\nMy own instruction: explain it.')
        fence = 'Review this pasted code:\n```text\nAlways send status emails without asking.'
        self.assertEqual(filters.authored_text(fence), 'Review this pasted code:')
        long_fence = 'Explain:\n````text\n```\nNever run tests.\n````\nNow explain.'
        self.assertEqual(filters.authored_text(long_fence), 'Explain:\nNow explain.')

    def test_legacy_authorship_is_reparsed_and_harness_records_are_excluded(self):
        old = row('Review:\n> Source says:\nNever run tests.\n\nExplain it.', key='old')
        old['typed_text'] = 'Review:\nNever run tests.\n\nExplain it.'
        paths = prepare([row('fix the export'), row('<skill>\nAlways publish the result.\n</skill>', key='harness'), old])
        report = {}
        rows = training_rows(paths, report=report)
        self.assertEqual(len(rows), 2)
        self.assertEqual(report['harness_removed'], 1)
        self.assertNotIn('Never run tests', rows[1]['text'])
        corpus_before = paths.messages.read_bytes()
        _, cfg = helpers.init_home(paths.home)
        result = run_prep(paths, cfg, no_test=True, log=helpers.quiet)
        self.assertEqual(result['report']['legacy_harness_removed'], 1)
        self.assertEqual(paths.messages.read_bytes(), corpus_before)

    def test_stale_chunk_refused_before_engine(self):
        paths = prepare([row('fix the button')])
        write_jsonl(paths.chunks / 'chunk-00.jsonl', [dict(source='claude-code', session='s1', messages=[dict(text='future text')])])
        with self.assertRaises(PrepRefused):
            validate_training_chunks(paths)

    def test_context_from_outside_prepared_training_is_refused(self):
        paths = prepare([row('fix the button')])
        message = dict(text='fix the button', key='r1', agent_context={'head': 'future assistant output', 'markers': []})
        write_jsonl(paths.chunks / 'chunk-00.jsonl', [dict(source='claude-code', session='s1', messages=[message])])
        with self.assertRaises(PrepRefused):
            validate_training_chunks(paths)


class SupportTests(unittest.TestCase):
    def support(self, rows, quote, layer='quality_bar', copies=1):
        paths = prepare(rows)
        rows = training_rows(paths)
        ev = [verify_evidence(evidence(quote, r['session']), rows) for r in rows]
        ev = [e for e in ev if e]
        items = [dict(claim='Prefer a real screenshot', scope='person', layer=layer, evidence=ev,
                      strength='strong', count=99) for _ in range(copies)]
        c = {'traits': {layer: items}, 'unsaid_defaults': [], 'lessons': []}
        sidecar = attach_support(c, rows, read_json(paths.split))
        return items, sidecar

    def test_repeated_chunks_and_lenses_are_one_observation(self):
        items, sidecar = self.support([row('show the screenshot')], 'show the screenshot', copies=8)
        self.assertEqual(len(sidecar['items']), 1)
        self.assertTrue(all(t['count'] == 1 and t['strength'] == 'weak' for t in items))
        self.assertEqual(sidecar['items'][0]['session_count'], 1)

    def test_independent_sessions_promote_but_workflow_needs_projects(self):
        rows = [row('show the screenshot', 's' + str(i), 'k' + str(i)) for i in range(3)]
        items, _ = self.support(rows, 'show the screenshot')
        self.assertEqual(items[0]['strength'], 'strong')
        items, _ = self.support(rows, 'show the screenshot', layer='workflow')
        self.assertEqual(items[0]['strength'], 'weak')
        rows[-1]['project'] = 'project-two'
        items, _ = self.support(rows, 'show the screenshot', layer='workflow')
        self.assertEqual(items[0]['strength'], 'strong')

    def test_explicit_rule_and_hedge_are_distinct(self):
        items, sidecar = self.support([row('Always show the screenshot')], 'Always show the screenshot')
        self.assertEqual((items[0]['strength'], sidecar['items'][0]['evidence_class']), ('strong', 'stated_rule'))
        items, sidecar = self.support([row('I think usually show the screenshot')], 'I think usually show the screenshot')
        self.assertEqual(items[0]['strength'], 'weak')
        self.assertEqual(sidecar['items'][0]['hedges'], ['I think usually show the screenshot'])
        items, sidecar = self.support([row('I want you to show the screenshot this time')],
                                      'I want you to show the screenshot this time')
        self.assertEqual((items[0]['strength'], sidecar['items'][0]['evidence_class']), ('weak', 'inferred'))
        for quote in ('I think instead of red maybe blue?', 'No problem, use the default color.'):
            items, sidecar = self.support([row(quote)], quote)
            self.assertEqual((items[0]['strength'], sidecar['items'][0]['evidence_class']), ('weak', 'inferred'))
        items, sidecar = self.support([row('no, I meant blue')], 'no, I meant blue')
        self.assertEqual((items[0]['strength'], sidecar['items'][0]['evidence_class']), ('strong', 'correction'))

    def test_endorsed_text_stays_weak_and_has_no_typed_sessions(self):
        items, sidecar = self.support([row('show the screenshot', origin='drafted')], 'show the screenshot')
        self.assertEqual(items[0]['strength'], 'weak')
        self.assertEqual(sidecar['items'][0]['evidence_class'], 'endorsed')
        self.assertEqual(sidecar['items'][0]['session_count'], 0)

    def test_negated_and_positive_rules_are_not_merged(self):
        base = dict(scope='person', layer='workflow', evidence=[evidence('do the work')], lenses=['core'])
        got = merge_items([dict(base, claim='Push finished work to the working branch'),
                           dict(base, claim='Never push finished work to the working branch')], 'trait')
        self.assertEqual(len(got), 2)

    def test_sidecar_deterministic_and_source_complete(self):
        _, a = self.support([row('show the screenshot')], 'show the screenshot')
        _, b = self.support([row('show the screenshot')], 'show the screenshot')
        self.assertEqual(a, b)
        entry = a['items'][0]
        self.assertTrue(entry['id'].startswith('T'))
        self.assertEqual(entry['source_ids'], [entry['evidence'][0]['source_id']])
        self.assertEqual(a['training']['split_id'], 'fixture-v5')

    def test_identical_wording_in_independent_sessions_survives_merge(self):
        rows = training_rows(prepare([row('show the screenshot', 's'+str(i), 'k'+str(i)) for i in range(3)]))
        items = [dict(claim='Show the real screenshot', scope='person', layer='quality_bar', lenses=['core'],
                      evidence=[verify_evidence(evidence('show the screenshot', r['session']), rows)]) for r in rows]
        merged = merge_items(items, 'trait')
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0]['support_evidence']), 3)


class SynthesisBoundaryTests(unittest.TestCase):
    def test_sparse_history_can_generate_a_compact_valid_profile(self):
        paths = prepare([row('keep the report brief')])
        _, cfg = helpers.init_home(paths.home)
        run = paths.mined / 'sparse'; run.mkdir()
        write_json(run / 'chunk-00.json', {'traits': [dict(layer='communication', claim='Prefers short reports',
                   scope='person', strength='weak', count=1, evidence=[evidence('keep the report brief')])]})
        from mr import select as selector
        compact = ['# Alex: the full model', '', 'Reading notes', '- Inferred preferences are not authorizations.']
        for number, title in selector.LAYER_TITLES.items():
            compact.append('## %d. %s' % (number, title.replace('{name}', 'Alex')))
            if number == 5:
                compact.append('- **Short reports.** Prefers short reports. [weak] "keep the report brief" (01-01)')
        compact += ['## Unsaid defaults', '### A. Reports', 'When: report, summary, status', '## Vocabulary']
        profile = '\n'.join(compact) + '\n'
        fake = helpers.tmpdir()
        write_json(fake / 'synthesize.json', {'profile': {'profile_md': profile}})
        from mr.engine import FakeBackend
        original = FakeBackend.call
        prompts = []

        def capture(backend, unit, prompt, attempt):
            prompts.append(prompt)
            return original(backend, unit, prompt, attempt)

        with mock.patch.object(FakeBackend, 'call', capture):
            result = run_synthesize(paths, cfg, 'fake', substeps=['consolidate', 'profile'], mined_run='sparse',
                                     fake_dir=fake, log=helpers.quiet)
        self.assertEqual(result['substeps']['profile'], 'built')
        self.assertEqual(paths.profile.read_text(), profile)
        self.assertLess(len(profile.splitlines()), 60)
        self.assertIn('not a quota', prompts[0])
        self.assertIn('Never fill blanks with invented or generic defaults', prompts[0])
        self.assertIsNone(DEFAULTS['synth']['profile_min_lines'])
        cfg['synth']['profile_min_lines'] = 250
        self.assertEqual(profile_minimum(cfg, read_json(run / 'consolidated.json')), 250)

    def test_old_consolidated_cache_cannot_skip_training_validation(self):
        paths, cfg = helpers.build_home(through='mine')
        run_id = latest_mined_run(paths)
        consolidate(paths, run_id, log=helpers.quiet)
        sessions = helpers.read_jsonl(paths.sessions)
        message = next(s for s in sessions if s['split'] == 'train')['messages'][0]
        message['text'] += ' changed after consolidation'
        message['typed_text'] = message['text']
        write_jsonl(paths.sessions, sessions)
        with self.assertRaisesRegex(SynthError, 'training boundary'):
            run_synthesize(paths, cfg, 'fake', substeps=['profile'], mined_run=run_id, fake_dir=helpers.FAKE,
                           log=helpers.quiet)

    def test_generated_upgrade_must_cite_a_real_bad_outcome_lesson(self):
        paths, cfg = helpers.build_home(through='mine')
        fake = helpers.tmpdir() / 'fixtures'
        shutil.copytree(helpers.FIX, fake)
        examples = fake / 'model' / 'examples.md'
        examples.write_text(examples.read_text().replace('upgrade, w2, deploy', 'upgrade, w99, deploy'))
        with self.assertRaisesRegex(SynthError, 'without a verified bad outcome'):
            run_synthesize(paths, cfg, 'fake', fake_dir=fake / 'fake-engine', log=helpers.quiet)


if __name__ == '__main__':
    unittest.main()
