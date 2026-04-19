#!/usr/bin/env node
/**
 * vmCode - npm wrapper for Python application
 * This script launches the Python vmcode application
 */

const { spawn } = require('child_process');
const os = require('os');
const path = require('path');
const fs = require('fs');

// Get the package directory - handle both local and global npm installs
// For global installs, the wrapper is typically in node_modules/.bin or npm's global bin directory
let packageDir = __dirname;

// First check if package.json exists in current directory (local install)
if (!fs.existsSync(path.join(packageDir, 'package.json'))) {
  // For global installs: check if we're in node_modules/.bin (go up to node_modules/package-name)
  const nodeModulesBin = path.join(packageDir, '..', 'vmcode-cli', 'package.json');
  if (fs.existsSync(nodeModulesBin)) {
    packageDir = path.join(packageDir, '..', 'vmcode-cli');
  } else {
    // Alternative: walk up the directory tree looking for package.json
    let found = false;
    let searchDir = packageDir;
    while (searchDir !== path.dirname(searchDir)) {
      const pkgJson = path.join(searchDir, 'package.json');
      if (fs.existsSync(pkgJson)) {
        packageDir = searchDir;
        found = true;
        break;
      }
      searchDir = path.dirname(searchDir);
    }
    if (!found) {
      console.error('Error: Could not find package.json. Installation may be corrupted.');
      console.error(`Searched from: ${__dirname}`);
      process.exit(1);
    }
  }
}

const pythonScript = path.join(packageDir, 'src', 'ui', 'main.py');

function findPython() {
  const possibleCommands = ['python3', 'python', 'python3.9', 'python3.10', 'python3.11', 'python3.12'];
  
  for (const cmd of possibleCommands) {
    try {
      const result = spawnSync(cmd, ['--version'], { stdio: 'ignore' });
      if (result.status === 0) {
        return cmd;
      }
    } catch (e) {
      // Continue to next command
    }
  }
  
  return null;
}

function spawnSync(command, args, options) {
  const { spawnSync: sync } = require('child_process');
  return sync(command, args, options);
}

function checkPythonDependencies(pythonCmd) {
  // Check if requirements are installed
  const requirementsFile = path.join(packageDir, 'requirements.txt');
  
  if (!fs.existsSync(requirementsFile)) {
    return true; // No requirements file, assume OK
  }
  
  try {
    // Try importing a key module to check if dependencies are installed
    const result = spawnSync(pythonCmd, ['-c', 'import rich, requests, yaml, readability, html2text, ddgs, pathspec, prompt_toolkit, pygments'], {
      stdio: 'ignore',
      cwd: packageDir
    });
    
    return result.status === 0;
  } catch (e) {
    return false;
  }
}

function installPythonDependencies(pythonCmd) {
  console.log('Installing Python dependencies...');
  const requirementsFile = path.join(packageDir, 'requirements.txt');
  
  const installProcess = spawn(pythonCmd, ['-m', 'pip', 'install', '-r', requirementsFile], {
    stdio: 'inherit',
    cwd: packageDir
  });
  
  return new Promise((resolve, reject) => {
    installProcess.on('close', (code) => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`Failed to install Python dependencies (exit code ${code})`));
      }
    });
  });
}

function showSetupMessage() {
  console.log('\n' + '='.repeat(60));
  console.log('vmCode - Terminal-based AI coding assistant');
  console.log('='.repeat(60));
  console.log('\nFirst-time setup needed!\n');
  console.log('1. Python is required (3.9 or later)');
  console.log('2. Python dependencies need to be installed\n');
  console.log('To complete setup, run:');
  console.log('  npm run install\n');
  console.log('Or manually:');
  console.log('  python3 -m pip install -r requirements.txt\n');
  console.log('Then run vmcode again.\n');
}

function ensureUserConfig() {
  // User config lives in ~/.vmcode/config.yaml (persists across npm updates)
  const vmcodeDir = path.join(os.homedir(), '.vmcode');
  const configFile = path.join(vmcodeDir, 'config.yaml');
  const configExample = path.join(packageDir, 'config.yaml.example');

  if (!fs.existsSync(vmcodeDir)) {
    fs.mkdirSync(vmcodeDir, { recursive: true });
  }

  if (!fs.existsSync(configFile)) {
    if (fs.existsSync(configExample)) {
      try {
        fs.copyFileSync(configExample, configFile);
        console.log('✓ Config created: ~/.vmcode/config.yaml');
      } catch (e) {
        console.log('Failed to create config:', e.message);
      }
    }
  }

  return configFile;
}

async function main() {
  // Find Python executable
  const pythonCmd = findPython();
  
  if (!pythonCmd) {
    console.error('\n❌ Error: Python 3.9+ is not installed or not in PATH');
    console.error('Please install Python from https://python.org\n');
    process.exit(1);
  }
  
  console.log(`✓ Using Python: ${pythonCmd}`);
  
  // Check and install Python dependencies
  if (!checkPythonDependencies(pythonCmd)) {
    console.log('\n⚠️  Python dependencies not installed');
    try {
      await installPythonDependencies(pythonCmd);
      console.log('✓ Python dependencies installed\n');
    } catch (e) {
      console.error('\n❌ Failed to install dependencies:', e.message);
      console.error('Try running: npm run install\n');
      process.exit(1);
    }
  }
  
  // Ensure user config exists in ~/.vmcode/ (persists across npm updates)
  const userConfigPath = ensureUserConfig();
  
  // Run the Python application
  // VMCODE_CONFIG_PATH points to user's persistent config.yaml in ~/.vmcode/
  const pythonProcess = spawn(pythonCmd, [pythonScript], {
    stdio: 'inherit',
    cwd: process.cwd(),
    env: {
      ...process.env,
      VMCODE_CONFIG_PATH: userConfigPath,
    }
  });
  
  pythonProcess.on('close', (code) => {
    process.exit(code || 0);
  });
  
  pythonProcess.on('error', (err) => {
    console.error('\n❌ Failed to start vmcode:', err.message);
    process.exit(1);
  });
  
  // Forward signals
  process.on('SIGINT', () => {
    pythonProcess.kill('SIGINT');
  });
  
  process.on('SIGTERM', () => {
    pythonProcess.kill('SIGTERM');
  });
}

// Run main function
main().catch(err => {
  console.error('\n❌ Unexpected error:', err);
  process.exit(1);
});
