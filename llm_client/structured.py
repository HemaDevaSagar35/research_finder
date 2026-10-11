"""Build Responses API requests without embedding schemas in user messages."""
import re


def structured_request(prompt=None, *, messages=None, system=None, schema):
    definition = schema.model_json_schema() if hasattr(schema, 'model_json_schema') else schema
    if not isinstance(definition, dict):
        raise TypeError('schema must be a JSON Schema object or Pydantic model class')
    if messages is not None and prompt is not None:
        raise ValueError('Pass prompt or messages, not both')
    instructions = [system] if system else []
    inputs = []
    for message in messages or []:
        if message['role'] == 'system':
            instructions.append(message['content'])
        else:
            inputs.append(dict(message))
    if messages is None:
        inputs = prompt or ''
    name = re.sub(r'[^A-Za-z0-9_-]', '_', definition.get('title', 'expected_output'))[:64]
    return dict(input=inputs, instructions='\n\n'.join(instructions) or None,
                text={'format': {'type': 'json_schema', 'name': name, 'schema': definition}})


def complete_text(result):
    if result.finish_reason not in ('completed', 'stop'):
        raise ValueError(f'Structured response did not complete: {result.finish_reason}')
    if not result.text.strip():
        raise ValueError('Structured response is empty or refused')
    return result.text
