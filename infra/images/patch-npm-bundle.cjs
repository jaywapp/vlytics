// Replace only reviewed npm bundled packages with compatible security patches.
const fs = require('node:fs');
const path = require('node:path');

if (process.argv.length !== 4) {
  throw new Error('Expected npm bundle and patch installation paths');
}
const npmRoot = fs.realpathSync(process.argv[2]);
const patchesRoot = fs.realpathSync(process.argv[3]);
const npmMetadata = require(path.join(npmRoot, 'package.json'));
if (npmMetadata.name !== 'npm' || npmMetadata.version !== '12.1.0') {
  throw new Error('Unexpected npm bundle');
}
const semver = require(path.join(npmRoot, 'node_modules', 'semver'));
const versions = { 'brace-expansion': '5.0.11', undici: '6.28.1' };
for (const [name, version] of Object.entries(versions)) {
  const source = path.join(patchesRoot, name);
  const target = path.join(npmRoot, 'node_modules', name);
  const metadata = require(path.join(source, 'package.json'));
  const original = require(path.join(target, 'package.json'));
  if (metadata.name !== name || metadata.version !== version || original.name !== name ||
      semver.major(original.version) !== semver.major(version)) {
    throw new Error('Unexpected bundled package or patch version');
  }
  for (const [dependency, range] of Object.entries(metadata.dependencies || {})) {
    const installed = require(path.join(npmRoot, 'node_modules', dependency, 'package.json'));
    if (!semver.satisfies(installed.version, range)) {
      throw new Error('Bundled dependency does not satisfy the patch requirement');
    }
  }
  fs.rmSync(target, { recursive: true });
  fs.cpSync(source, target, { recursive: true });
  console.log(`${name}: ${version}`);
}
