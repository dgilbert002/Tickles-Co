/**
 * Helper to spawn Python processes with correct environment.
 * On Windows: prefers a real interpreter from %LOCALAPPDATA%\\Programs\\Python\\Python3xx
 * or the `py -3` launcher before falling back to ./venv — so the app works without
 * activating or fixing a broken venv when system Python is installed.
 */
import { spawn, ChildProcess } from 'child_process';
import { existsSync } from 'node:fs';
import path, { dirname } from 'path';
import { fileURLToPath } from 'url';

export interface PythonSpawnOptions {
  args?: string[];
  cwd?: string;
  env?: Record<string, string | undefined>;
  timeout?: number;
}

/**
 * Spawn a Python 3 process using {@link resolvePythonBinary} rules.
 * @param scriptPath - Path to Python script relative to project root
 * @param options - Spawn options
 * @returns ChildProcess
 */
const moduleDir = dirname(fileURLToPath(import.meta.url));

function resolveProjectRoot() {
  if (process.env.PROJECT_ROOT?.trim()) {
    return process.env.PROJECT_ROOT;
  }
  return path.resolve(moduleDir, '..');
}

export type ResolvedPython = {
  command: string;
  /** Inserted before the script path (e.g. ['-3'] for `py -3 script.py`) */
  preArgs: string[];
};

function resolvePythonBinary(projectRoot: string): ResolvedPython {
  const configured =
    process.env.PYTHON_BIN ||
    process.env.PYTHON_PATH ||
    process.env.PYTHON_BINARY;
  if (configured?.trim()) {
    return { command: configured.trim(), preArgs: [] };
  }

  if (process.platform === 'win32') {
    const localApp = process.env.LOCALAPPDATA || '';
    // Prefer newest store-style installs (python.org / Microsoft Store) — real python.exe
    const storeVersions = [
      'Python314',
      'Python313',
      'Python312',
      'Python311',
      'Python310',
    ];
    for (const ver of storeVersions) {
      const candidate = path.join(localApp, 'Programs', 'Python', ver, 'python.exe');
      if (existsSync(candidate)) {
        return { command: candidate, preArgs: [] };
      }
    }

    // Launcher on PATH (no venv required). We do not default to ./venv so a broken stub
    // does not override system Python; use PYTHON_BIN or a fresh venv for isolation.
    return { command: 'py', preArgs: ['-3'] };
  }

  if (process.platform === 'darwin') {
    return { command: '/usr/local/bin/python3', preArgs: [] };
  }

  return { command: '/usr/bin/python3', preArgs: [] };
}

function normalizePathOverride() {
  if (process.env.PYTHON_PATH_OVERRIDE) {
    return process.env.PYTHON_PATH_OVERRIDE;
  }
  return '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin';
}

/** Project-local packages: `pip install --target python_packages` (no venv). Prepended to PYTHONPATH. */
function localPythonPackagesDir(projectRoot: string): string {
  return path.join(projectRoot, 'python_packages');
}

function prependPythonPath(
  env: Record<string, string | undefined>,
  projectRoot: string,
): void {
  const local = localPythonPackagesDir(projectRoot);
  if (!existsSync(local)) {
    return;
  }
  const sep = path.delimiter;
  // Use only env.PYTHONPATH (cleanEnv already merged process.env; non-Win may have cleared it)
  const prev = env.PYTHONPATH?.trim() || '';
  env.PYTHONPATH = prev ? `${local}${sep}${prev}` : local;
}

export function spawnPython(scriptPath: string, options: PythonSpawnOptions = {}): ChildProcess {
  const { args = [], cwd, env = {}, timeout } = options;
  const projectRoot = resolveProjectRoot();
  const { command: pythonBinary, preArgs } = resolvePythonBinary(projectRoot);
  const scriptAbsolute = path.isAbsolute(scriptPath)
    ? scriptPath
    : path.join(projectRoot, scriptPath);

  const cleanEnv = {
    ...process.env,
    ...env,
  };

  // Only override PATH on non-Windows platforms unless explicitly disabled
  const shouldOverridePath =
    process.platform !== 'win32' && process.env.PYTHON_KEEP_PATH !== 'true';

  if (shouldOverridePath) {
    cleanEnv.PATH = normalizePathOverride();
    cleanEnv.PYTHONPATH = '';
    cleanEnv.PYTHONHOME = undefined;
  }

  // Local `pip install --target python_packages` (no venv) — must run after PATH/PYTHONPATH reset above
  prependPythonPath(cleanEnv, projectRoot);

  const spawnArgs = [...preArgs, scriptAbsolute, ...args];

  return spawn(pythonBinary, spawnArgs, {
    cwd: cwd || projectRoot,
    env: cleanEnv,
    timeout,
  });
}
