"""UNWIRED bridge-owned structured producer SOURCE; never a legacy Codex parser.

Only trusted bridge bootstrap may supply the private owner object, configured bot,
actual Registry and current-origin predicate. No transport/client/human/report
handler imports this module. Capability is a proposed local owner interface only.
"""
import json
from dataclasses import dataclass
from feishu_bridge.questions import codex_questions as q
from feishu_bridge.questions import question_defaults_source as draft

CAPABILITY = 'bridge-owner-original-ui-defaults-v1-SOURCE'
BINDING_FIELDS = ('work', 'display', 'thread', 'turn', 'runtime', 'protocol', 'connection')


def _text(value, limit):
    return type(value) is str and bool(value.strip()) and len(value) <= limit


@dataclass(frozen=True)
class _PreparedQuestion:
    producer: object
    status: str
    card: object = None
    frozen: str = ''
    binding: object = None
    snapshot: tuple = ()


class BridgeOwnedQuestionProducer:
    def __init__(self, registry, *, owner, bot, valid):
        if type(registry) is not q.Registry or type(owner) is not object or not _text(bot, 256) or not callable(valid):
            raise ValueError('trusted owner bootstrap required')
        self.registry, self._owner, self.bot, self.valid = registry, owner, bot, valid
        self._frames = {}
        self._bindings = {}
        self._origins = {}
        self._cards = {}
        self._identities = {}

    def _current(self, card):
        try:
            return self.valid(card) is True
        except Exception:
            return False

    def _prune(self):
        self.registry.sweep(self._current)
        for aid in list(self._frames):
            if aid not in self.registry.cards:
                self._frames.pop(aid, None)
                self._bindings.pop(aid, None)
                self._origins.pop(aid, None)
                self._cards.pop(aid, None)
                self._identities.pop(aid, None)

    def prepare_owner_question(self, owner, *, capability, origin, process,
                            source_binding, item_id, questions, defaults):
        """Prepare a frozen original/optional frame, without admission or ask emit.

        The only optional_size_refused result is a positively current full frame
        whose exact serialized bytes exceed the unchanged bound before any write.
        """
        if owner is not self._owner or type(capability) is not str or capability != CAPABILITY:
            return _PreparedQuestion(self, 'refused')
        if (type(origin) is not q.Origin or type(origin.binding) is not q.Binding
                or process is not origin.process or origin.failed or origin.normal or origin.disconnected
                or type(source_binding) is not dict or any(type(k) is not str for k in source_binding)
                or set(source_binding) != set(BINDING_FIELDS)
                or any(not _text(source_binding[k], 256) for k in BINDING_FIELDS)
                or source_binding != {k: getattr(origin.binding, k) for k in BINDING_FIELDS}
                or origin.binding.protocol != q.PROTOCOL or not _text(item_id, 256)
                or item_id in origin.seen or len(origin.seen) >= 4096):
            return _PreparedQuestion(self, 'refused')
        binding = origin.binding
        if type(questions) is not list or not 1 <= len(questions) <= 3:
            return _PreparedQuestion(self, 'refused')
        parsed = []
        for i, question in enumerate(questions, 1):
            if (type(question) is not dict or any(type(k) is not str for k in question)
                    or set(question) != {'title', 'options'}):
                return _PreparedQuestion(self, 'refused')
            title, options = question['title'], question['options']
            if (not _text(title, 6000) or type(options) is not list or not 2 <= len(options) <= 8
                    or any(not _text(label, 1024) for label in options)
                    or len({label.strip() for label in options}) != len(options)):
                return _PreparedQuestion(self, 'refused')
            parsed.append((f'Q{i}', title, tuple(options)))
        if type(defaults) is not list or len(defaults) != len(parsed):
            return _PreparedQuestion(self, 'refused')
        for i, value in enumerate(defaults, 1):
            if (type(value) is not dict or any(type(k) is not str for k in value)
                    or set(value) != {'header', 'optionIndex', 'label', 'role'}
                    or type(value['header']) is not str or value['header'] != f'Q{i}'
                    or type(value['optionIndex']) is not int
                    or type(value['label']) is not str or type(value['role']) is not str
                    or value['role'] not in ('approval', 'choice')):
                return _PreparedQuestion(self, 'refused')
        now = self.registry.wall()
        card = q.Card(q.PREFIX + q.secrets.token_hex(16), origin, item_id, tuple(parsed),
                      q.secrets.token_hex(16), self.registry.clock() + q.TTL, now, now + q.TTL)
        if (not self._current(card) or origin.binding is not binding
                or process is not origin.process or origin.normal or origin.disconnected or origin.failed):
            return _PreparedQuestion(self, 'refused')
        self._prune()
        if (len(self.registry.cards) >= q.MAX_PENDING
                or any(old.origin.binding.work == origin.binding.work for old in self.registry.cards.values())):
            return _PreparedQuestion(self, 'refused')  # Does not displace an existing decision; legacy conflicts remain unchanged.
        frame = {'ev': 'ask', 'ask_id': card.ask_id, 'bot': self.bot,
                 'chat_id': origin.binding.display,
                 'questions': [{'header': h, 'question': title, 'multiSelect': False,
                                'options': [{'label': label, 'description': ''} for label in options]}
                               for h, title, options in card.questions],
                 'created_at': card.created_at, 'expires_at': card.expires_at,
                 'question_binding': card.wire_binding()}
        basis = draft.original_basis(frame, now=now)
        declaration = {'version': 1, 'basis': basis, 'defaults': defaults}
        if draft.producer_default_draft(frame, declaration, capability=draft.CAPABILITY,
                                        current=True, now=now) is None:
            return _PreparedQuestion(self, 'refused')
        frame['question_ui_defaults'] = declaration  # STAGED field; no live API allocation.
        frozen = json.dumps(frame, ensure_ascii=False, separators=(',', ':'))
        snapshot = (origin, card.questions, card.item, card.binding_id,
                    card.created_at, card.expires_at, card.deadline, process, card.ask_id)
        if len(frozen.encode()) > q.MAX_CARD_BYTES:
            return _PreparedQuestion(self, 'optional_size_refused', card, frozen, binding, snapshot)
        # No await or callback between final validity guard and canonical admission.
        if (not self._current(card) or origin.binding is not binding
                or process is not origin.process or origin.normal or origin.disconnected or origin.failed):
            return _PreparedQuestion(self, 'refused')
        return _PreparedQuestion(self, 'ready', card, frozen, binding, snapshot)

    def _prepared_current(self, owner, prepared):
        if (owner is not self._owner or type(prepared) is not _PreparedQuestion
                or prepared.producer is not self or type(prepared.status) is not str
                or prepared.status not in ('ready', 'optional_size_refused')
                or type(prepared.snapshot) is not tuple or len(prepared.snapshot) != 9
                or type(prepared.frozen) is not str or type(prepared.binding) is not q.Binding):
            return False
        card = prepared.card
        if (type(card) is not q.Card or not self._current(card)
                or card.origin is not prepared.snapshot[0]
                or card.questions is not prepared.snapshot[1]
                or card.origin.process is not prepared.snapshot[7]
                or card.origin.binding is not prepared.binding
                or type(card.ask_id) is not str or card.ask_id != prepared.snapshot[8]
                or card.origin.normal or card.origin.failed or card.origin.disconnected
                or card.item in card.origin.seen or len(card.origin.seen) >= 4096
                or card.state != 'pending' or card.answer != '' or card.user != ''):
            return False
        values = (card.item, card.binding_id, card.created_at, card.expires_at, card.deadline)
        if any(type(a) is not type(b) or a != b for a, b in zip(values, prepared.snapshot[2:7])):
            return False
        if not (self.registry.clock() < card.deadline
                and card.created_at <= self.registry.wall() < card.expires_at):
            return False
        return (len(self.registry.cards) < q.MAX_PENDING
                and not any(old.origin.binding.work == card.origin.binding.work
                            for old in self.registry.cards.values()))

    def optional_size_refusal(self, owner, prepared):
        # Never classify an admission/emit result (None) as a pre-write refusal.
        return (self._prepared_current(owner, prepared)
                and prepared.status == 'optional_size_refused'
                and len(prepared.frozen.encode()) > q.MAX_CARD_BYTES)

    def admit_prepared_question(self, owner, prepared):
        if (not self._prepared_current(owner, prepared) or prepared.status != 'ready'
                or len(prepared.frozen.encode()) > q.MAX_CARD_BYTES):
            return None
        self._prune()
        if not self._prepared_current(owner, prepared):
            return None  # First TTL/scope/object/decision/capacity recheck, no renewal.
        card, frozen, binding = prepared.card, prepared.frozen, prepared.binding
        origin, item_id = card.origin, card.item
        origin.seen.add(item_id)
        self.registry.cards[card.ask_id] = card
        self._frames[card.ask_id] = frozen
        self._bindings[card.ask_id] = binding
        self._origins[card.ask_id] = origin
        self._cards[card.ask_id] = card
        self._identities[card.ask_id] = (card.questions, card.item, card.binding_id,
                                       card.created_at, card.expires_at, card.deadline)
        try:
            self.registry.emit(json.loads(frozen))
        except Exception:
            self.registry.cancel(card, 'card_send_failed')
            self._frames.pop(card.ask_id, None)
            self._bindings.pop(card.ask_id, None)
            self._origins.pop(card.ask_id, None)
            self._cards.pop(card.ask_id, None)
            self._identities.pop(card.ask_id, None)
            return None
        self.registry._event(card, 'emitted_unconfirmed')
        return card

    def emit_owner_question(self, owner, **kwargs):
        # Compatibility caller: refusal/possible write still returns None without fallback.
        prepared = self.prepare_owner_question(owner, **kwargs)
        return self.admit_prepared_question(owner, prepared)

    def replay_metadata(self, owner, ask_id):
        """Read-only current pending snapshot, not a resend or authority restoration."""
        if owner is not self._owner or type(ask_id) is not str:
            return None
        self._prune()
        card = self.registry.cards.get(ask_id)
        if (card is None or card is not self._cards.get(ask_id) or card.state != 'pending'
                or card.origin is not self._origins.get(ask_id)
                or card.origin.disconnected
                or card.origin.failed or card.origin.binding is not self._bindings.get(ask_id)
                or not self._current(card)):
            return None
        snapshot = self._identities[ask_id]
        current = (card.questions, card.item, card.binding_id,
                   card.created_at, card.expires_at, card.deadline)
        if (current[0] is not snapshot[0]
                or any(type(a) is not type(b) or a != b for a, b in zip(current[1:], snapshot[1:]))):
            return None
        frozen = self._frames.get(ask_id)
        return json.loads(frozen) if frozen is not None else None
