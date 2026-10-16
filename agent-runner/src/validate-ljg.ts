// Compiled together with unmodified upstream code: no Bun installation or global config.
import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {validate} from '../../skills/ljg-paper/scripts/validate_note';
import {LJG_ADAPTER_VERSION, LJG_PROJECTION_VERSION, projectLJGMarkdown} from './ljg-projection.js';
const [note, mapFlag, map] = process.argv.slice(2);
if (!note || mapFlag !== '--map' || !map) {
  process.stderr.write('Usage: node validate-ljg.js NOTE --map PAPER_MAP\n');process.exit(2);
}
const noteBytes = readFileSync(note);
const mapBytes = readFileSync(map);
const sha256 = (value: string | Buffer) => createHash('sha256').update(value).digest('hex');
const adapter = {
  version: LJG_ADAPTER_VERSION, projection: LJG_PROJECTION_VERSION,
  note_sha256: sha256(noteBytes), map_sha256: sha256(mapBytes),
  projection_sha256: null as string | null, upstream_validator_executed: false,
};
let result;
try {
  const projection = projectLJGMarkdown(noteBytes.toString('utf8'));
  adapter.projection_sha256 = sha256(projection);
  result = validate(projection, note, mapBytes.toString('utf8'));
  adapter.upstream_validator_executed = true;
} catch (error) {
  result = {ok: false, errors: [`Markdown 兼容层：${error instanceof Error ? error.message : '无法生成校验投影'}`], warnings: [], stats: {}};
}
process.stdout.write(JSON.stringify({...result, adapter})+'\n');process.exitCode=result.ok?0:1;
