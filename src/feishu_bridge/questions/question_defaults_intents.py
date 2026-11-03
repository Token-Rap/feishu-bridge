"""SOURCE-only POSIX owner intent inbox; no transport or model authoring API.

The managed local owner authors immutable files explicitly. JSON author strings
are not bot authentication: the named private directory/file ownership boundary
is trusted local account ownership, not cryptographic or HUMAN authority.
"""
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from feishu_bridge.questions import codex_questions as q
from feishu_bridge.questions import question_defaults_producer as producer

VERSION = 1
AUTHOR = 'local-owner'
DIRECTORY = 'question-ui-default-intents'
MAX_FILES = 128
MAX_BYTES = 24000
BINDING = ('work', 'display', 'thread', 'turn', 'runtime', 'protocol')
_KEYS = {'version','author','bot','intent_id','created_at','expires_at',
         'binding','process_id','questions','defaults'}


def _text(value, limit=256):
    return type(value) is str and bool(value.strip()) and len(value) <= limit


def _questions(value):
    if type(value) is not list or not 1 <= len(value) <= 3:
        return False
    for question in value:
        if (type(question) is not dict or any(type(k) is not str for k in question)
                or set(question) != {'title','options'} or not _text(question['title'],6000)
                or type(question['options']) is not list or not 2 <= len(question['options']) <= 8
                or any(not _text(s,1024) for s in question['options'])
                or len({s.strip() for s in question['options']}) != len(question['options'])):
            return False
    return True


def validate_intent(value, *, bot, now):
    """Closed explicit owner data; no raw exception/body/prose inference."""
    if (type(value) is not dict or any(type(k) is not str for k in value)
            or set(value) != _KEYS or type(value['version']) is not int or value['version'] != VERSION
            or type(value['author']) is not str or value['author'] != AUTHOR
            or type(value['bot']) is not str or value['bot'] != bot
            or type(value['intent_id']) is not str or re.fullmatch('[0-9a-f]{32}',value['intent_id']) is None
            or type(value['process_id']) is not int or value['process_id'] <= 0):
        return False
    created,expires=value['created_at'],value['expires_at']
    try:
        if (any(type(n) not in (int,float) or not math.isfinite(n) for n in (now,created,expires))
                or not created <= now < expires or not 0 < expires-created <= q.TTL):
            return False
    except OverflowError:
        return False
    binding=value['binding']
    if (type(binding) is not dict or any(type(k) is not str for k in binding)
            or set(binding) != set(BINDING) or any(not _text(binding[k]) for k in BINDING)
            or binding['protocol'] != q.PROTOCOL or not _questions(value['questions'])):
        return False
    defaults=value['defaults']
    if type(defaults) is not list or len(defaults) != len(value['questions']):
        return False
    for i,(question,d) in enumerate(zip(value['questions'],defaults),1):
        if (type(d) is not dict or any(type(k) is not str for k in d)
                or set(d) != {'header','optionIndex','label','role'}
                or type(d['header']) is not str or d['header'] != f'Q{i}'
                or type(d['optionIndex']) is not int or not 0 <= d['optionIndex'] < len(question['options'])
                or type(d['label']) is not str or d['label'] != question['options'][d['optionIndex']]
                or type(d['role']) is not str or d['role'] not in ('approval','choice')):
            return False
    try:
        return len(json.dumps(value,ensure_ascii=False).encode()) <= MAX_BYTES
    except (TypeError,ValueError,OverflowError):
        return False


def _private(st, mode):
    return (hasattr(os,'geteuid') and st.st_uid == os.geteuid()
            and stat.S_IMODE(st.st_mode) == mode)


def _identity(st):
    return (st.st_dev,st.st_ino,st.st_uid,st.st_mode,st.st_nlink,
            st.st_size,st.st_mtime_ns,st.st_ctime_ns)


def _sync_root(root):
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        if not _private(os.fstat(fd),0o700):
            raise ValueError('owner directory changed')
        os.fsync(fd)
    finally:
        os.close(fd)


class OwnerIntentInbox:
    def __init__(self, root, *, bot, audit, wall=time.time):
        self.root=Path(root);self.bot=bot;self.audit=audit;self.wall=wall

    def _note(self, text):
        try:self.audit(text)
        except Exception:pass  # Fixed metadata only; retained intent is the disposition.

    def _root(self):
        try:
            st=self.root.lstat()
            return stat.S_ISDIR(st.st_mode) and _private(st,0o700)
        except OSError:
            return False  # No directory creation, ACL fallback or implicit capability.

    def try_event(self, event, *, origin, process, current, adapter, owner):
        """Called by actual current stdout observer, before legacy observe.

        True consumes the matched NEW event even if its sole emit failed; no
        fallback retry through the legacy issuer after a possible write.
        """
        if (not self._root() or current is not True or type(origin) is not q.Origin
                or origin.normal or origin.failed or origin.disconnected or process is not origin.process
                or type(event) is not dict or 'id' in event or event.get('replay') is True
                or event.get('method') != 'item/completed'):
            return False
        params=event.get('params');item=params.get('item') if type(params) is dict else None
        if (type(params) is not dict or params.get('threadId') != origin.binding.thread
                or params.get('turnId') != origin.binding.turn or type(item) is not dict
                or item.get('type') != 'agentMessage' or not _text(item.get('id'))
                or item['id'] in origin.seen or len(origin.seen)>=4096 or not _questions(item.get('questions'))):
            return False
        pid=getattr(process,'pid',None)  # Trusted current process handle, never client data.
        if type(pid) is not int or pid <= 0:
            return False
        claimed=False
        claim_attempted=False
        fallback_proved=False
        try:
            paths=list(self.root.iterdir())
            if len(paths)>MAX_FILES:
                return False
            matches=[]
            for path in paths:
                if re.fullmatch('[0-9a-f]{32}\\.json',path.name) is None:
                    continue
                st=path.lstat()
                if (not stat.S_ISREG(st.st_mode) or not _private(st,0o400)
                        or st.st_nlink != 1 or st.st_size > MAX_BYTES):
                    continue
                fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
                with os.fdopen(fd,'rb') as stream:
                    opened=os.fstat(stream.fileno());raw=stream.read(MAX_BYTES+1)
                if _identity(opened) != _identity(st) or _identity(path.lstat()) != _identity(st) or len(raw)>MAX_BYTES:
                    continue
                try:value=json.loads(raw)
                except (ValueError,UnicodeError):continue
                if (not validate_intent(value,bot=self.bot,now=self.wall())
                        or path.name != value['intent_id']+'.json'
                        or value['process_id'] != pid
                        or value['binding'] != {k:getattr(origin.binding,k) for k in BINDING}
                        or value['questions'] != item['questions']):
                    continue
                matches.append((path,st,value))
            if len(matches)!=1:
                return False  # Unknown/ambiguous inputs cannot mint defaults.
            path,st,value=matches[0]
            prepared=adapter.prepare_owner_question(owner,capability=producer.CAPABILITY,
                origin=origin,process=process,
                source_binding={k:getattr(origin.binding,k) for k in producer.BINDING_FIELDS},
                item_id=item['id'],questions=value['questions'],defaults=value['defaults'])
            size_refused=adapter.optional_size_refusal(owner,prepared)
            # Durable at-most-once intent claim. Claimed records are not card/grant
            # authority and are never replayed after restart. Retain errors honestly.
            used=self.root/(('refused.' if size_refused else 'used.')+value['intent_id']+'.json')
            claim_attempted=True
            os.link(path,used,follow_symlinks=False)  # EEXIST refuses; never overwrite/retry.
            claimed=True
            if path.lstat().st_ino != st.st_ino or path.lstat().st_dev != st.st_dev:
                self._note('question default intent refused reason=claim_source_changed')
                return True
            path.unlink()
            _sync_root(self.root)  # Persist claim/removal before a possible emitted write.
            if not validate_intent(value,bot=self.bot,now=self.wall()):
                self._note('question default intent refused reason=expired_after_claim')
                return True
            if size_refused:
                if not adapter.optional_size_refusal(owner,prepared):
                    self._note('question default intent refused reason=prewrite_scope_changed')
                    return True
                fallback_proved=True
                self._note('question default intent refused reason=optional_size_original_fallback')
                return False  # Exactly unchanged Registry.observe creates the complete original once.
            card=adapter.admit_prepared_question(owner,prepared)
            self._note('question default intent consumed result='+('admitted_unconfirmed' if card is not None else 'refused_or_unknown'))
            return True
        except Exception:
            self._note('question default intent refused reason=owner_io_unknown')
            return claim_attempted  # Claim-I/O uncertainty is never legacy fallback authority.
        finally:
            if claim_attempted and not fallback_proved:origin.seen.add(item['id'])  # Unknown claim/emit cannot become a legacy retry.


def author_intent(home, *, bot, value, now=None):
    """Explicit managed local owner interface; not exposed on any transport.

    The operator supplies the exact current-turn IDs/PID and complete NEW-event
    text/options/defaults. There is no automatic extraction from legacy model text.
    No authoring call is made by the bridge, client, injection or report handler.
    """
    now=time.time() if now is None else now
    if not hasattr(os,'geteuid') or not validate_intent(value,bot=bot,now=now):
        raise ValueError('invalid explicit owner intent')
    root=Path(home)/DIRECTORY
    if not root.exists():root.mkdir(mode=0o700)
    st=root.lstat()
    if not stat.S_ISDIR(st.st_mode) or not _private(st,0o700) or len(list(root.iterdir()))>=MAX_FILES:
        raise ValueError('owner intent directory unavailable')
    path=root/(value['intent_id']+'.json');used=root/('used.'+value['intent_id']+'.json')
    if used.exists() or (root/('refused.'+value['intent_id']+'.json')).exists():raise ValueError('intent already consumed or refused')
    raw=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o400)
    with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    _sync_root(root)
    return path


def main():
    import argparse,sys
    parser=argparse.ArgumentParser(description='Managed local owner explicit question UI intent authoring; not a human answer')
    parser.add_argument('--home',required=True);parser.add_argument('--bot',required=True)
    args=parser.parse_args()
    value=json.loads(sys.stdin.buffer.read(MAX_BYTES+1))
    author_intent(args.home,bot=args.bot,value=value)
    print('owner presentation intent authored; no card/answer/grant sent')


if __name__=='__main__':main()
