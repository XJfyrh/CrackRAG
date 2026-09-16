"""Resume a single persisted Probe round without repeating settled phases.

The Go grant carries the original capability, durable counters and observations.
Missing/uncertain prior results never authorize a replacement model request.
"""
import json

import grpc
from crackrag.v1 import runtime_pb2 as pb

from .config import ROOT
from .model import Model, contract_output_tokens
from .m2 import decode, payload


async def resume_probe(agent, batch, report):
    independent = None
    try:
        agent.check()
        configuration = json.loads(agent.contract.configuration_json or '{}')
        limit = configuration.get('max_probe_model_calls', 2)
        if configuration.get('probe_enabled') is False or limit == 0:
            return 'PROBE_DISABLED_BY_CONTRACT'
        if type(limit) is not int or not 0 <= limit <= 2:
            raise ValueError('UNSUPPORTED_PROBE_CONTRACT')
        grant = decode(await agent.control.BeginProbe(pb.BatchRequest(
            context=agent.context, batch_id=batch, stop_reason='M4_RECOVERY'),
            metadata=agent.metadata, timeout=10))
        if grant.get('unresolved'):
            raise ValueError('PROBE_RECOVERY_OUTCOME_UNRESOLVED')
        used = grant.get('models_used', 0)
        tools_used = grant.get('tools_used', 0)
        if type(used) is not int or not 0 <= used <= 2 or type(tools_used) is not int or not 0 <= tools_used <= 4:
            raise ValueError('PROBE_RECOVERY_COUNTER_INVALID')
        prior = {}
        for call in grant.get('settled_calls', []):
            phase = call.get('phase')
            if phase not in ('select', 'inspect') or phase in prior or not isinstance(call.get('raw_result'), str):
                raise ValueError('PROBE_RECOVERY_OUTCOME_UNRESOLVED')
            prior[phase] = call['raw_result']
        if len(prior) != used or ('inspect' in prior and 'select' not in prior):
            raise ValueError('PROBE_RECOVERY_OUTCOME_UNRESOLVED')
        allowed = grant['region_ids']
        hypotheses = [{'candidate': item['candidate'], 'reasons': item['reasons']}
                      for item in grant['doubts']]
        system = (ROOT / 'ai-runtime/prompts/m2-v1/probe.txt').read_text(encoding='utf-8')

        async def ask(phase, context, mock_action):
            nonlocal independent, used
            agent.check()
            if used >= limit or agent.model_count >= agent.contract.max_model_calls:
                return None
            if independent is None:
                independent = Model(agent.settings, agent.tools, agent.context, agent.metadata)
                independent.contract = agent.contract
                independent.m3_call = {
                    **getattr(getattr(agent, 'model', None), 'm3_call', {}),
                    'subexperiment': agent.m2_config.get('subexperiment', 'quality')}
            used += 1
            agent.model_count += 1
            text, _ = await independent.ask(payload(system, {'phase': phase, **context},
                max_output_tokens=contract_output_tokens(agent.contract)), mock_action,
                stage='probe', batch_id=batch, probe_token=grant['probe_token'])
            return text

        selected = prior.get('select')
        if selected is None:
            selected = await ask('select', {'hypotheses': hypotheses, 'allowed_region_ids': allowed},
                                 {'action': 'open', 'region_id': allowed[0]})
            if selected is None:
                return 'PROBE_MODEL_CALL_LIMIT'
        action = json.loads(selected)
        if not isinstance(action, dict) or set(action) != {'action', 'region_id'} or action['action'] != 'open' or action['region_id'] not in allowed:
            return 'PROBE_ABSTAINED_OR_INVALID_ACTION'

        observations = [item for item in grant.get('observations', [])
                        if isinstance(item, dict) and item.get('region_id') == action['region_id']]
        # An already-settled inspect phase must have a persisted observation.
        # Never issue an OpenDocument solely to repair inconsistent audit state.
        if 'inspect' in prior:
            if not observations:
                raise ValueError('PROBE_RECOVERY_OBSERVATION_MISSING')
            return 'PROBE_OBSERVED'
        if not observations:
            if tools_used >= 4:
                return 'PROBE_TOOL_LIMIT'
            probe_context = pb.RequestContext()
            probe_context.CopyFrom(agent.context)
            probe_context.service_id = 'python-probe'
            opened = await agent.probe_tools.OpenDocument(pb.ProbeOpenRequest(
                context=probe_context, batch_id=batch, probe_token=grant['probe_token'],
                region_ids=[action['region_id']]), metadata=agent.metadata, timeout=10)
            from .agent import source
            observations = [source(region) for region in opened.regions]
        inspected = await ask('inspect', {'hypotheses': hypotheses, 'observations': observations},
            {'action': 'conclude', 'summary': 'Fresh source observed; ambiguity remains for deterministic verification.'})
        return 'PROBE_OBSERVED' if inspected is not None else 'PROBE_MODEL_CALL_LIMIT'
    except grpc.aio.AioRpcError as exc:
        if 'M4_PROBE_OUTCOME_UNRESOLVED' in (exc.details() or ''):
            raise ValueError('PROBE_RECOVERY_OUTCOME_UNRESOLVED') from exc
        return 'PROBE_UNAVAILABLE_OR_UNRESOLVED'
    except (KeyError, TypeError, json.JSONDecodeError):
        return 'PROBE_ABSTAINED_OR_INVALID_ACTION'
    finally:
        record = getattr(independent, 'last_record', {})
        if getattr(agent.context, 'job_id', '') and record.get('http_dispatched') and record.get('cost', {}).get('amount') is None:
            agent.m3_unknown_call = record
        if independent:
            await independent.close()
