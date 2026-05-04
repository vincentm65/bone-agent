## bone-agent

A CLI-based AI coding assistant capable of codebase search, file editing, computer use, and web search.

<img width="1850" height="396" alt="image" src="https://github.com/user-attachments/assets/4f20cc22-a7d9-4423-afbf-15bbe1e29890" />

## Features

- **Multiple LLM Provider Support**: bone-agent (built-in proxy), OpenAI, Anthropic, OpenRouter, GLM, Gemini, Kimi, MiniMax, and local models
- **Tool-Based Interaction**: Code search (`rg`), file editing, directory operations, and web search
- **Multiple Modes**: Edit (full access), Plan (read-only), and Learn (documentation style)
- **Parallel Execution**: Run multiple tools concurrently for efficiency
- **Swarm Pool**: Delegate work to parallel worker agents via `/swarm` — admin mode with task decomposition and independent workers
- **Conversation History**: Markdown logging with context compaction
- **Approval Workflows**: Safety checks for dangerous commands

## Installation

### Option 1: npm install (Recommended)

```bash
# Install globally (requires Python 3.9+)
npm install -g bone-agent

# Run bone
bone
```

Or use npx without installing:

```bash
npx bone
```

### What Gets Installed

The npm package automatically:
1. Checks for Python 3.9+ on your system
2. Installs Python dependencies via pip
3. Creates `~/.bone/config.yaml` from `config.yaml.example` if missing (persists across updates)
4. Sets up the `bone` command globally

**Requirements:**
- Node.js 14+ (for npm)
- Python 3.9+ (for the application)
- pip (to install Python dependencies)

If Python is not found, the installer will guide you through installing it.

### Option 2: Git Clone

```bash
# Clone the repository
git clone https://github.com/vincentm65/bone-agent.git
cd bone-agent

# Install Python dependencies
pip install -r requirements.txt

# Run bone-agent
python src/ui/main.py
```

**Requirements:**
- Python 3.9+
- pip (to install Python dependencies)

## Configuration

### Setting API Keys

You have three options to set your API keys:

#### Option 1: Interactive Commands (Recommended)

Run the app and use the built-in commands:
```
> /key sk-your-api-key-here
> /provider openai
```

#### Option 2: Edit config.yaml Directly

Edit `~/.bone/config.yaml` and add your keys:

```yaml
# OpenAI
OPENAI_API_KEY: "sk-your-key-here"
OPENAI_MODEL: gpt-4o-mini

# Anthropic (Claude)
ANTHROPIC_API_KEY: "sk-ant-your-key-here"
ANTHROPIC_MODEL: claude-3-5-sonnet-20241022

# Or any other supported provider...
```

**Note:** Config is stored at `~/.bone/config.yaml` — it persists across npm updates and is never tracked by git.

#### Option 3: Environment Variables

Set environment variables (they take precedence over ~/.bone/config.yaml):

```bash
export OPENAI_API_KEY="sk-your-key-here"
export ANTHROPIC_API_KEY="sk-ant-your-key-here"

bone
```

### Available Environment Variables

- `ANTHROPIC_API_KEY` - Anthropic (Claude) API key
- `OPENAI_API_KEY` - OpenAI API key
- `GLM_API_KEY` - GLM (Zhipu AI) API key
- `GEMINI_API_KEY` - Google Gemini API key
- `OPENROUTER_API_KEY` - OpenRouter API key
- `KIMI_API_KEY` - Kimi (Moonshot AI) API key
- `MINIMAX_API_KEY` - MiniMax API key
- `BONE_API_KEY` - bone-agent (proxy) API key (auto-set via `/signup`)
- `BONE_API_BASE` - bone-agent (proxy) API base URL (default: `https://api.vmcode.dev`)

## Swarm Pool

The swarm pool lets you delegate work to parallel worker agents. The main agent enters **admin mode** where it researches, decomposes tasks, and dispatches them to independent workers that execute with full tool access.

```
> /swarm start <name>       Start a swarm (enters admin mode)
> /swarm join <name>        Turn this session into a worker
> /swarm status             Show worker/task snapshot
> /swarm close              Stop server, exit admin mode
```

Workers run in separate terminals with full REPL access. File edits are auto-approved; commands (shell execution) require admin approval. Workers are fresh `AgenticOrchestrator` instances with isolated context — they receive self-contained task prompts from the admin's research phase.

**Caveats (v1):**
- Admin process exit ends the swarm; no admin reclaim in v1.
- No hard file locks — task prompts declare write scope, but workers have full tool access.
- Worker edits are auto-approved to avoid serial bottlenecks.
- Commands require approval by the admin agent.
- Worker crash marks task interrupted, not requeued.
- Worker manual intervention during a task marks the result as review-needed.

## Commands

- `/provider <name>` - Switch LLM provider
- `/model <name>` - Set model for current provider
- `/key <api_key>` - Set API key for current provider
- `/mode <edit|plan|learn>` - Switch interaction mode
- `/config` - Show all configuration settings
- `/signup <email>` - Create a bone-agent account and get API key
- `/account` - View your bone-agent account and plan details
- `/plan` - View available plans and pricing
- `/upgrade` - Upgrade your subscription
- `/skills list [query]` - List saved skills, optionally filtered by name or content
- `/skills show <name>` - Display a saved skill
- `/skills add <name>` - Create a reusable prompt skill in your editor
- `/skills edit <name>` - Open a saved skill in your editor
- `/skills modify <name> [prompt]` - Update an existing saved skill inline or in your editor
- `/skills load <name>` - Load a saved skill into the current chat
- `/skills use <name>` - Alias for `/skills load`
- `/skills remove <name>` - Delete a saved skill
- `/skills dir` - Print the skills directory path
- `/help` - Display all available commands

Example:

```text
/skills add frontend_design
/skills modify frontend_design Use restrained, production-quality UI patterns.
/skills use frontend_design
```

/help Menu:
<img width="1843" height="1349" alt="image" src="https://github.com/user-attachments/assets/631ab805-f012-4bb6-a031-c82a339e94c5" />


## Project Structure

```
bone-agent/
├── bin/
│   ├── npm-wrapper.js  # npm entry point
│   ├── rg              # ripgrep binary (Linux/macOS)
│   └── rg.exe          # ripgrep binary (Windows)
├── config.yaml.example # Configuration template
├── requirements.txt    # Python dependencies
├── package.json        # npm package definition
├── .npmignore          # npm package exclusions
├── .gitignore          # git exclusions
├── src/
│   ├── core/           # Core orchestration and state management
│   ├── llm/            # LLM client and provider configurations
│   ├── ui/             # CLI interface and commands
│   └── utils/          # Utilities (file ops, search, validation)
└── tests/              # Test suite (for development)
```

## bone-agent Plan (Built-in Proxy)

bone-agent offers a built-in proxy provider for a seamless setup experience. Create an account and start coding without configuring third-party API keys.

```
> /signup you@example.com
```

Available plans: **Free**, **Lite**, and **Pro**. Use `/plan` to see details and `/upgrade` to change plans.

*Paid plans coming soon.*

## Security

- User config lives at `~/.bone/config.yaml` — outside the repo and git, persists across updates
- Never commit API keys or sensitive configuration
- Use environment variables for CI/CD or shared environments

## Development

bone-agent is currently in active development. Production readiness is in progress with focus on:
- Comprehensive test coverage
- Documentation
- Error handling improvements
- Performance optimizations

## Swarm hands-off approval validation

Practical scenarios for the admin agent approving or denying worker commands
through the hands-off approval loop (`handle_approval`).

### Safe scoped command — approve

Worker asks for a logical, in-scope shell command that advances the task. Examples:

- Create or remove a temp directory under `.temp/`.
- Run a safe read-only command (e.g., `ls .temp/`, `ping -c 1 host`).
- Write or edit a file within the task's declared write scope.

**Procedure:** Inspect the pending approval, verify the command matches scope and is not
destructive, then call `handle_approval(task_id=..., call_id=..., approved=True)`.
Optionally include a `reason` note.

### Unsafe or out-of-scope command — deny

Worker requests a destructive, broad, unrelated, or out-of-scope command. Examples:

- `rm -rf` outside `.temp/` or the task's scope.
- Broad git operations (`git reset --hard`, `git push --force`).
- File writes to paths not in the task's write scope.
- Network exfiltration or data scraping commands.

**Procedure:** Confirm unsafe scope, then call
`handle_approval(task_id=..., call_id=..., approved=False, reason=...)` with a clear
explanation and safer guidance.

### Denial recovery

After a denial, the worker receives the reason and instructions to revise. It should not
treat the denial as opaque failure or retry the same command. Verify the worker references
the denial reason and proposes a revised, safer approach.

### Fallback: human intervention

If the admin agent's approval logic is uncertain or blocked, the human can resolve the
pending item by giving the admin agent explicit approval or denial instructions.
