"""Pure synthetic SOURCE proofs; no live SDK, Registry or producer invocation."""
import copy
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('question_defaults_source', Path(__file__).resolve().parents[2] / 'src/feishu_bridge/questions/question_defaults_source.py')
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


def inputs():
    card = {'ev': 'ask', 'ask_id': 'codex-question-SYNTHETIC', 'bot': 'backend',
            'chat_id': 'group:fleet-product', 'created_at': 1000, 'expires_at': 1300,
            'question_binding': dict(zip(M.BINDING_KEYS, ['b', 't', 'turn', 'item', M.PROTOCOL])),
            'questions': [{'header': f'Q{i}', 'question': f'Full synthetic question {i}',
                           'multiSelect': False, 'options': [{'label': label, 'description': ''}
                                                           for label in ['原始批准完整标签', '原始拒绝完整标签']]}
                          for i in (1, 2)]}
    declaration = {'version': 1, 'basis': M.original_basis(card, now=1100),
                   'defaults': [{'header': f'Q{i}', 'optionIndex': 0,
                                 'label': '原始批准完整标签', 'role': 'approval'} for i in (1, 2)]}
    return card, declaration


def run(card, declaration, **kw):
    return M.producer_default_draft(card, declaration, capability=kw.get('capability', M.CAPABILITY),
                                    current=kw.get('current', True), now=kw.get('now', 1100))


def test_complete_original_declaration_projects_without_answer_or_mutation():
    card, d = inputs(); old = copy.deepcopy((card, d)); result = run(card, d)
    assert result == {'schema': M.DRAFT_SCHEMA, 'basis': d['basis'],
                      'selections': [{k: q[k] for k in ('header', 'optionIndex', 'label')} for q in d['defaults']]}
    assert (card, d) == old and 'answers' not in result
    result['basis'][0] = 'changed'; assert d['basis'][0] == card['chat_id']


def test_prose_position_or_unknown_capability_never_supplies_default():
    card, d = inputs()
    assert run(card, None) is None
    assert run(card, d, capability=M.PROTOCOL) is None
    assert run(card, d, current=None) is None
    d['defaults'][0].pop('role'); assert run(card, d) is None


def test_complete_binding_original_and_ttl_basis_is_exact():
    for index in range(7):
        card, d = inputs(); d['basis'][index] = 'different'; assert run(card, d) is None
    card, d = inputs(); card['questions'][0]['options'].reverse(); assert run(card, d) is None


def test_partial_reordered_or_extra_defaults_are_atomic_refusals():
    card, d = inputs(); d['defaults'].pop(); assert run(card, d) is None
    card, d = inputs(); d['defaults'].reverse(); assert run(card, d) is None
    card, d = inputs(); d['extra'] = True; assert run(card, d) is None


def test_index_and_role_do_not_accept_alias_or_boolean():
    for index in (-1, 2, True, '0'):
        card, d = inputs(); d['defaults'][0]['optionIndex'] = index; assert run(card, d) is None
    card, d = inputs(); d['defaults'][0]['role'] = 'Approved (Recommended)'; assert run(card, d) is None
    card, d = inputs(); d['defaults'][0]['label'] = '批准'; assert run(card, d) is None


def test_expiry_terminal_and_security_namespace_cannot_gain_selection():
    card, d = inputs(); assert run(card, d, now=1300) is None
    card, d = inputs(); card['answered'] = True; assert run(card, d) is None
    card, d = inputs(); card['ask_id'] = 'approval-security'; assert run(card, d) is None


def test_malformed_ambiguous_or_oversized_original_is_refused():
    card, d = inputs(); card['questions'][0]['options'][1]['label'] = ' 原始批准完整标签 '; assert run(card, d) is None
    card, d = inputs(); card['question_binding']['extra'] = 'x'; assert run(card, d) is None
    card, d = inputs(); card['created_at'] = float('nan'); assert run(card, d) is None
    card, d = inputs(); card['unrelated'] = 'x' * 33000; assert run(card, d) is None


def test_old_title_options_parser_and_live_production_imports_remain_unchanged():
    root = Path(__file__).resolve().parents[2]
    for name in ('fleet_bridge.py', 'app_frontend.py', 'codex_questions.py'):
        assert 'question_defaults_source' not in (root / "src/feishu_bridge" / ("questions" if name == "codex_questions.py" else "bridge") / name).read_text()
    assert 'set(q) - {"title", "options"}' in (root / 'src/feishu_bridge/questions/codex_questions.py').read_text()


def test_basis_bool_numeric_substitution_reproduced_then_refused():
    card, d = inputs(); assert run(card, d) is not None
    for value in (0, 0.0):
        bad = copy.deepcopy(d); bad['basis'][6][0][2] = value
        assert run(card, bad) is None
    card, d = inputs(); card['created_at'] = 1; card['expires_at'] = 301
    d['basis'] = M.original_basis(card, now=100); assert run(card, d, now=100) is not None
    d['basis'][3] = True; assert run(card, d, now=100) is None


def test_basis_rejects_malformed_nonfinite_and_custom_comparison():
    card, d = inputs()
    for value in (None, {}, tuple(d['basis']), False, 'original'):
        bad = copy.deepcopy(d); bad['basis'] = value; assert run(card, bad) is None
    for value in (float('nan'), float('inf'), -float('inf'), 10 ** 10000, '1000', False):
        bad = copy.deepcopy(d); bad['basis'][3] = value; assert run(card, bad) is None
    class Trap:
        def __eq__(self, other): raise AssertionError('custom comparison executed')
    bad = copy.deepcopy(d); bad['basis'][6][0][2] = Trap(); assert run(card, bad) is None


def test_finite_numeric_timestamps_keep_js_projection_semantics():
    card, d = inputs(); d['basis'][3] = 1000.0; d['basis'][4] = 1300.0
    assert run(card, d) is not None
    card, d = inputs(); card['created_at'] = 1000.125
    d['basis'] = M.original_basis(card, now=1100); assert run(card, d) is not None
    d['basis'][3] = 1000.25; assert run(card, d) is None
