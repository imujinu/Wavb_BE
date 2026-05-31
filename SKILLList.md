### andrej-karpathy-skills
/plugin marketplace add forrestchang/andrej-karpathy-skills
/plugin install andrej-karpathy-skills@karpathy-skills

### superpower
/plugin marketplace add obra/superpowers-marketplace
/plugin install superpowers@superpowers-marketplace

### understand



/plugin marketplace add Lum1104/Understand-Anything
/plugin install understand-anything

--------- uder stand 명령어 -----------

# Ask anything about the codebase
/understand-chat How does the payment flow work?

# Analyze impact of your current changes
/understand-diff

# Deep-dive into a specific file or function
/understand-explain src/auth/login.ts

# Generate an onboarding guide for new team members
/understand-onboard

# Extract business domain knowledge (domains, flows, steps)
/understand-domain

# Analyze a Karpathy-pattern LLM wiki knowledge base
/understand-knowledge ~/path/to/wiki

# Re-run anytime — incremental by default (only re-analyzes changed files)
/understand

# Auto-update on every commit via a post-commit hook
/understand --auto-update

# Scope to a subdirectory (for huge monorepos)
/understand src/frontend

-------------------------------