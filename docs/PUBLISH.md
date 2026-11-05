# Putting this on GitHub

The repository is already a git repository with its history committed (branch `main`). You only need to give it a home.

## Easiest: the script
Double-click `Publish_to_GitHub.bat` in the repository folder. It needs [Git for Windows](https://git-scm.com/download/win).
If you also have the GitHub CLI (`gh`), it logs you in if needed and creates a **private** repository called `Jarvis` and pushes.
Without `gh` it prints the two commands for the manual way below.

## By hand
1. On github.com click **New repository**, name it `Jarvis`, choose Private (you can change it later), and **do not**
   tick "add a README" - it must start empty. Click Create.
2. In a terminal in this folder:
   ```
   git remote add origin https://github.com/YOUR-NAME/Jarvis.git
   git push -u origin main
   ```
3. GitHub asks you to sign in the first time (a browser window, or a personal access token if you use a terminal-only login).

## Before you make it public
* Add a `LICENSE` file (see `NOTICE.md`). Without one, nobody else is allowed to reuse your code.
* The commits are authored as `Jarvis <jarvis@users.noreply.github.com>`. To make them yours:
  `git rebase -r --root --exec "git commit --amend --reset-author --no-edit"` after setting `git config user.name` and `user.email`.
* Search the folder once for anything personal that should not be there (the `.gitignore` already keeps `config/`, `cache/`
  and `models/` out, and nothing in the repository contains an API key).
