#!/usr/bin/env node
/**
 * vmCode npm install script
 * Handles Python dependency installation
 */

const { spawn, spawnSync } = require('child_process');
const os = require('os');
const path = require('path');
const fs = require('fs');

const packageDir = path.resolve(__dirname, '..');
const requirementsFile = path.join(packageDir, 'requirements.txt');

console.log('Installing vmCode...\n');

function findPython() {
  const possibleCommands = ['python3', 'python', 'python3.9', 'python3.10', 'python3.11', 'python3.12'];
  
  for (const cmd of possibleCommands) {
    try {
      const result = spawnSync(cmd, ['--version'], { stdio: 'pipe' });
      if (result.status === 0) {
        const version = result.stdout.toString() || result.stderr.toString();
        console.log(`✓ Found ${cmd}: ${version.trim()}`);
        return cmd;
      }
    } catch (e) {
      // Continue to next command
    }
  }
  
  return null;
}

function installDependencies(pythonCmd) {
  if (!fs.existsSync(requirementsFile)) {
    console.log('⚠️  No requirements.txt found, skipping Python dependencies');
    return Promise.resolve();
  }
  
  console.log('\nInstalling Python dependencies...\n');
  
  return new Promise((resolve, reject) => {
    const installProcess = spawn(pythonCmd, ['-m', 'pip', 'install', '-r', requirementsFile], {
      stdio: 'inherit',
      cwd: packageDir
    });
    
    installProcess.on('close', (code) => {
      if (code === 0) {
        console.log('\n✓ Python dependencies installed successfully\n');
        resolve();
      } else {
        reject(new Error(`pip install failed with exit code ${code}`));
      }
    });
    
    installProcess.on('error', (err) => {
      reject(new Error(`Failed to run pip: ${err.message}`));
    });
  });
}

function setupConfig() {
  // User config lives in ~/.vmcode/config.yaml (persists across npm updates)
  const vmcodeDir = path.join(os.homedir(), '.vmcode');
  const configFile = path.join(vmcodeDir, 'config.yaml');
  const configExample = path.join(packageDir, 'config.yaml.example');

  if (!fs.existsSync(vmcodeDir)) {
    fs.mkdirSync(vmcodeDir, { recursive: true });
  }

  if (!fs.existsSync(configFile) && fs.existsSync(configExample)) {
    console.log('Creating config.yaml from example...');
    try {
      fs.copyFileSync(configExample, configFile);
      console.log('✓ Config created: ~/.vmcode/config.yaml');
      console.log('\n⚠️  IMPORTANT: Edit ~/.vmcode/config.yaml and add your API keys!');
      console.log('   Or set them via environment variables:\n');
      console.log('   export OPENAI_API_KEY="sk-your-key-here"\n');
    } catch (e) {
      console.log('⚠️  Failed to create config:', e.message);
    }
  }
}

async function main() {
  try {
    // Find Python
    const pythonCmd = findPython();
    
    if (!pythonCmd) {
      console.error('\n❌ Error: Python 3.9+ is not installed or not in PATH');
      console.error('Please install Python from https://python.org');
      console.error('\nAfter installing Python, run: npm install vmcode\n');
      process.exit(1);
    }
    
    // Check Python version
    const versionResult = spawnSync(pythonCmd, ['-c', 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'], {
      stdio: 'pipe'
    });
    
    if (versionResult.status === 0) {
      const version = versionResult.stdout.toString().trim();
      const [major, minor] = version.split('.').map(Number);
      
      if (major < 3 || (major === 3 && minor < 9)) {
        console.error(`\n❌ Error: Python 3.9+ is required (found ${version})`);
        console.error('Please install a newer version of Python from https://python.org\n');
        process.exit(1);
      }
    }
    
    // Install dependencies
    await installDependencies(pythonCmd);
    
    // Setup config
    setupConfig();
    
    console.log('='.repeat(60));
    console.log('vmCode installation complete!');
    console.log('='.repeat(60));
    console.log('\nRun vmcode with:');
    console.log('  vmcode\n');
    console.log('Or with npx:');
    console.log('  npx vmcode\n');
    
  } catch (err) {
    console.error('\n❌ Installation failed:', err.message);
    console.error('\nTry installing dependencies manually:');
    console.error('  python3 -m pip install -r requirements.txt\n');
    process.exit(1);
  }
}

main();
