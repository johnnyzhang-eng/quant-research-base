"""Private, finite paper-adapter runs; no implicit SDK connection or order retry."""
from __future__ import annotations

from datetime import datetime, timezone
from importlib.util import find_spec
from pathlib import Path
from uuid import uuid4

from .evidence import ContractError, append_event, canonical_hash, digest, load_json, seal_run, write_json
from .execution.futu_paper import FutuPaperAdapter, FutuPaperBinding, PaperRiskEnvelope
from .execution.alpaca_paper import AlpacaPaperAdapter, AlpacaPaperBinding

SCHEMA = 'official-paper-run/1'


def _private(path, root):
    original, root = Path(path).absolute(), Path(root).resolve()
    path = original.resolve()
    public = Path(__file__).resolve().parents[1]
    if path == root or not path.is_relative_to(root):
        raise ContractError('PRIVATE_DESCENDANT_REQUIRED')
    if path.is_relative_to(public) or root.is_relative_to(public):
        raise ContractError('ACCOUNT_EVIDENCE_MUST_STAY_OUTSIDE_PUBLIC_CHECKOUT')
    for cursor in (original, *original.parents):
        if cursor.resolve() == root:
            break
        if cursor.is_symlink():
            raise ContractError('SYMLINK_PRIVATE_PATH_NOT_ACCEPTED')
    return path


def validate_config(config):
    fields = {'schema_version', 'binding', 'envelope', 'state_file', 'action', 'intent', 'payload', 'fee_bound'}
    is_alpaca = isinstance(config, dict) and config.get('schema_version') == 'official-paper-run/2' and config.get('provider') == 'alpaca'
    if is_alpaca:
        fields.add('provider')
    if not isinstance(config, dict) or set(config) != fields or (not is_alpaca and config['schema_version'] != SCHEMA):
        raise ContractError('EXACT_PAPER_RUN_CONFIG_REQUIRED')
    try:
        binding_class = AlpacaPaperBinding if is_alpaca else FutuPaperBinding
        binding = binding_class(**dict(config['binding'],
            allowed_acc_ids=tuple(config['binding']['allowed_acc_ids']),
            allowed_codes=tuple(config['binding']['allowed_codes']))).validate()
        envelope = PaperRiskEnvelope(**config['envelope']).validate()
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError('INVALID_PAPER_BINDING_OR_ENVELOPE') from exc
    if not is_alpaca and binding.peer_host not in {'127.0.0.1', '::1', 'localhost'}:
        raise ContractError('FIRST_PROBE_REQUIRES_LOCAL_OPEND')
    if envelope.max_quantity != 1 or envelope.max_pending_orders != 1:
        raise ContractError('FIRST_PROBE_LIMIT_ONE_SHARE_ONE_PENDING')
    begin = datetime.fromisoformat(envelope.submission_start.replace('Z', '+00:00'))
    end = datetime.fromisoformat(envelope.submission_end.replace('Z', '+00:00'))
    if (end-begin).total_seconds() > 3600:
        raise ContractError('FIRST_PROBE_WINDOW_MAX_ONE_HOUR')
    if config['action'] not in {'preflight', 'recover', 'submit', 'cancel'}:
        raise ContractError('UNKNOWN_PAPER_ACTION')
    if not isinstance(config['state_file'], str) or not config['state_file'] or Path(config['state_file']).is_absolute() or '..' in Path(config['state_file']).parts:
        raise ContractError('RELATIVE_PRIVATE_STATE_FILE_REQUIRED')
    if config['action'] == 'preflight':
        if any(config[key] is not None for key in ('intent', 'payload', 'fee_bound')):
            raise ContractError('PREFLIGHT_CANNOT_CONTAIN_ORDER')
    else:
        if not isinstance(config['intent'], str) or not config['intent'].strip():
            raise ContractError('EXPLICIT_INTENT_REQUIRED')
        if config['action'] != 'submit' and (config['payload'] is not None or config['fee_bound'] is not None):
            raise ContractError('RECOVERY_OR_CANCEL_CANNOT_CONTAIN_NEW_ORDER')
        if config['action'] == 'submit' and (not isinstance(config['payload'], dict) or not isinstance(config['fee_bound'], dict)):
            raise ContractError('EXPLICIT_ORDER_AND_FEE_BOUND_REQUIRED')
    return binding, envelope


def local_readiness(config_path, private_root):
    """Local inventory only: SDK presence is not login, permission or peer proof."""
    path = _private(config_path, private_root)
    config = load_json(path)
    binding, _ = validate_config(config)
    is_alpaca = isinstance(binding, AlpacaPaperBinding)
    return {'schema_version': 'paper-local-readiness/1', 'config_sha256': digest(path),
            'provider': 'alpaca' if is_alpaca else 'futu',
            'sdk_import_discoverable': None if is_alpaca else find_spec('futu') is not None,
            'provider_contacted': False, 'send_attempts': 0,
            'login_verified': False, 'peer_attested': False,
            'settled_cash_verified': False, 'economic_reconciliation_verified': False,
            'classification': 'LOCAL_INVENTORY_ONLY_NOT_OFFICIAL_ACCEPTANCE',
            'required': (['private PAPER credentials and canonical allowed account UUID',
                         'fixed verified TLS paper origin; fresh ACTIVE USD paper account',
                         'flat unlevered paper account and finite one-share probe'] if is_alpaca else
                        ['reviewed installed SDK transport with actual socket peer attestation',
                         'fresh explicit SIMULATE account and US authorization',
                         'reviewed settled cash/sellable quantity and explicit fee bound'])}


def run_paper(config_path, private_root, output, *, transport, observed_at=None, clock=None):
    """Execute exactly one requested action on a caller-supplied reviewed transport.

    No network factory is supplied. No loops, waits, automatic cancellation,
    resubmission, cash resolver, real account fallback or background timer.
    Transport injection alone is never official/economic acceptance evidence.
    """
    config_path = _private(config_path, private_root)
    output = _private(output, private_root)
    config = load_json(config_path)
    binding, envelope = validate_config(config)
    state = _private(Path(private_root) / config['state_file'], private_root)
    for suffix in ('-wal', '-shm', '-journal'):
        _private(Path(str(state) + suffix), private_root)
    at = observed_at or datetime.now(timezone.utc).isoformat()
    folder = output / ('paper-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid4().hex[:12])
    registry = _private(output / 'registry.jsonl', private_root)
    folder.mkdir(parents=True, exist_ok=False)
    append_event(registry, {'event': 'STARTED', 'run_id': folder.name, 'at': at,
                           'config_sha256': canonical_hash(config), 'action': config['action']})
    write_json(folder / 'input.json', config)
    from .execution import futu_paper
    source_files = {'runner': Path(__file__), 'adapter': Path(futu_paper.__file__)}
    is_alpaca = isinstance(binding, AlpacaPaperBinding)
    if is_alpaca:
        from .execution import alpaca_paper
        source_files['alpaca_transport'] = Path(alpaca_paper.__file__)
    code_hashes = {key: digest(path) for key, path in source_files.items()}
    write_json(folder / 'code-hashes.json', code_hashes)
    state.parent.mkdir(parents=True, exist_ok=True)
    adapter = None
    result = {'schema_version': 'paper-run-result/1', 'action': config['action'],
              'provider': 'alpaca' if is_alpaca else 'futu',
              'status': 'NOT_STARTED', 'errors': [], 'official_chain_verified': False,
              'economic_reconciliation_verified': False, 'actual_fees': None,
              'goal_complete': False}
    try:
        adapter_class = AlpacaPaperAdapter if is_alpaca else FutuPaperAdapter
        adapter = adapter_class(state, transport, binding=binding, envelope=envelope,
                                   **({'clock': clock} if clock is not None else {}))
        # Every restart recovers unresolved intents before a new action. Absence
        # cannot trigger a send; adapter keeps the original cash reservation.
        previous = adapter.snapshot()
        before = len([r for r in previous['records'] if r['kind'] == 'BEFORE_PLACE'])
        recovery = []
        for intent in previous['intents']:
            if intent['state'] != 'TERMINAL':
                recovery.append(adapter.recover(intent['intent'], at=at))
        write_json(folder / 'restart-recovery.json', recovery)
        action = config['action']
        if action == 'preflight':
            response = adapter.preflight(at=at)
        elif action == 'recover':
            response = adapter.recover(config['intent'], at=at)
        elif action == 'cancel':
            adapter.recover(config['intent'], at=at)
            response = adapter.cancel(config['intent'], at=at)
        else:
            response = adapter.submit(config['intent'], config['payload'], at=at, fee_bound=config['fee_bound'])
        write_json(folder / 'response.json', response)
        if is_alpaca and config['action'] in {'recover', 'cancel'}:
            # A finite observation; it never clears OMS blockers or sends again.
            import json
            from .execution.alpaca_paper import reconcile_paper_probe
            local = adapter.intent(config['intent'])
            journal = adapter.snapshot()['records']
            preflights = [json.loads(r['payload']) for r in journal if r['kind'] == 'PREFLIGHT']
            queries = [json.loads(r['payload']) for r in journal if r['kind'] == 'ORDER_QUERY' and r['intent'] == config['intent']]
            orders = [o for page in queries for o in page if o.get('order_id') == local['order_id']]
            if local['order_id'] and preflights and orders:
                final = transport.snapshot(acc_id=binding.acc_id, trd_env='PAPER', at=at)
                activities = transport.activities(order_id=local['order_id'], all_account=True,
                    from_at=preflights[0]['request_started_at'], until_at=final['received_at'])
                observation = reconcile_paper_probe(preflights[0], orders[-1], final, activities)
                write_json(folder / 'paper-probe-evidence.json', {'before': preflights[0], 'order': orders[-1],
                    'after': final, 'activities': activities, 'observation': observation})
                result['paper_probe_observation_matched'] = observation['matched']
        result['status'] = 'ACTION_RETURNED_REVIEW_REQUIRED'
    except Exception as exc:
        result['status'] = 'BLOCKED_OR_UNCERTAIN'
        result['errors'].append({'exception_type': type(exc).__name__, 'reason': str(exc)})
    finally:
        if adapter is not None:
            snapshot = adapter.snapshot()
            write_json(folder / 'adapter-state.json', snapshot)
            after = len([r for r in snapshot['records'] if r['kind'] == 'BEFORE_PLACE'])
            result['durable_submission_intents_this_run'] = after - locals().get('before', after)
            result['unresolved_intents'] = sum(r['state'] != 'TERMINAL' for r in snapshot['intents'])
            result['blockers'] = snapshot['blockers']
            result['execution_records'] = snapshot['execution_records']
            adapter.close()
        if any(digest(path) != code_hashes[key] for key, path in source_files.items()):
            result['errors'].append({'reason': 'SOURCE_CHANGED_DURING_RUN'})
            result['status'] = 'REJECTED_CHANGED_SOURCE'
        write_json(folder / 'result.json', result)
        checksum = seal_run(folder)
        append_event(registry, {'event': 'FINISHED', 'run_id': folder.name,
                               'status': result['status'], 'seal_sha256': checksum})
    return int(bool(result['errors'])), folder


def run_alpaca(config_path, credentials_path, private_root, output):
    """Explicit finite network entry; never read credentials for another provider."""
    from .execution.alpaca_paper import AlpacaPaperTransport, PaperHTTP
    config_path = _private(config_path, private_root)
    binding, envelope = validate_config(load_json(config_path))
    if not isinstance(binding, AlpacaPaperBinding):
        raise ContractError('ALPACA_PAPER_CONFIG_REQUIRED')
    path = _private(credentials_path, private_root)
    if path.stat().st_mode & 0o077:
        raise ContractError('PRIVATE_CREDENTIAL_FILE_MODE_0600_REQUIRED')
    credentials = load_json(path)
    if not isinstance(credentials, dict) or set(credentials) != {'key_id', 'secret_key'}:
        raise ContractError('EXACT_PRIVATE_PAPER_CREDENTIAL_FIELDS_REQUIRED')
    transport = AlpacaPaperTransport(PaperHTTP(**credentials), binding=binding, envelope=envelope)
    return run_paper(config_path, private_root, output, transport=transport)
