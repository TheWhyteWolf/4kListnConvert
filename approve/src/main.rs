// SPDX-License-Identifier: GPL-3.0-or-later
//! A fast terminal approval queue for vidlib's --bloated conversion
//! candidates.
//!
//! This binary does no probing, filtering or encoding of its own: it shells
//! out to the already-tested Python `vidlib` tool for "what counts as
//! bloated" (`vidlib list --bloated`) and for the actual conversion
//! (`vidlib convert <approved paths> -y`), reading display fields straight
//! out of vidlib's own SQLite cache in between. The only new logic here is
//! the UI.

mod model;
mod ui;

use std::io;
use std::path::PathBuf;
use std::process::Command;

use anyhow::{Context, Result};
use crossterm::event::{self, Event, KeyCode, KeyEventKind};
use crossterm::execute;
use crossterm::terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode};
use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;

use model::Candidate;

struct Options {
    vidlib_bin: String,
    db: PathBuf,
    under: Option<PathBuf>,
    /// Flags forwarded verbatim to `vidlib convert` once the user approves.
    convert_args: Vec<String>,
}

fn parse_args() -> Result<Options> {
    let mut vidlib_bin = "vidlib".to_string();
    let mut db = model::default_db_path();
    let mut under = None;
    let mut convert_args = Vec::new();

    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--vidlib-bin" => vidlib_bin = args.next().context("--vidlib-bin needs a value")?,
            "--db" => db = PathBuf::from(args.next().context("--db needs a value")?),
            "--under" => under = Some(PathBuf::from(args.next().context("--under needs a value")?)),
            "--stereo" | "--all-subs" => convert_args.push(arg),
            "--sub-lang" | "--encoder" | "-e" | "--quality" | "-q" | "--preset" | "-p"
            | "--target-height" | "-t" => {
                convert_args.push(arg.clone());
                convert_args.push(args.next().with_context(|| format!("{arg} needs a value"))?);
            }
            "-h" | "--help" => {
                print_help();
                std::process::exit(0);
            }
            other => anyhow::bail!("unrecognised argument: {other} (try --help)"),
        }
    }
    Ok(Options { vidlib_bin, db, under, convert_args })
}

fn print_help() {
    println!(
        "vidlib-approve - fast approval queue for vidlib's --bloated candidates\n\n\
         Usage: vidlib-approve [options]\n\n\
         Options:\n\
         \x20 --db PATH            vidlib library database (default: XDG state dir)\n\
         \x20 --under DIR          only candidates under this directory\n\
         \x20 --vidlib-bin NAME    vidlib executable to call (default: vidlib on PATH)\n\
         \x20 --stereo             downmix audio to stereo on conversion\n\
         \x20 --all-subs           keep every subtitle regardless of language\n\
         \x20 --sub-lang LANG      keep only this subtitle language\n\
         \x20 -e, --encoder NAME   video encoder\n\
         \x20 -q, --quality N      CRF/CQ value\n\
         \x20 -p, --preset NAME    encoder preset\n\
         \x20 -t, --target-height N  output height (default 1080)\n\n\
         Keys: up/down move, space toggle, a approve all, x clear all,\n\
         \x20     enter convert approved, q quit.\n"
    );
}

pub struct App {
    candidates: Vec<Candidate>,
    cursor: usize,
    status: Option<String>,
}

impl App {
    fn new(candidates: Vec<Candidate>) -> Self {
        App { candidates, cursor: 0, status: None }
    }

    fn move_cursor(&mut self, delta: isize) {
        if self.candidates.is_empty() {
            return;
        }
        let len = self.candidates.len() as isize;
        let next = (self.cursor as isize + delta).clamp(0, len - 1);
        self.cursor = next as usize;
    }

    fn toggle_current(&mut self) {
        if let Some(c) = self.candidates.get_mut(self.cursor) {
            c.selected = !c.selected;
        }
    }

    fn set_all(&mut self, value: bool) {
        for c in &mut self.candidates {
            c.selected = value;
        }
    }

    fn approved(&self) -> Vec<&Candidate> {
        self.candidates.iter().filter(|c| c.selected).collect()
    }
}

/// What to do once the TUI loop exits.
enum Outcome {
    Quit,
    Convert,
}

fn run_app(terminal: &mut Terminal<CrosstermBackend<io::Stdout>>, app: &mut App) -> Result<Outcome> {
    loop {
        terminal.draw(|frame| ui::draw(frame, app))?;
        if let Event::Key(key) = event::read()? {
            if key.kind != KeyEventKind::Press {
                continue;
            }
            app.status = None;
            match key.code {
                KeyCode::Char('q') | KeyCode::Esc => return Ok(Outcome::Quit),
                KeyCode::Up | KeyCode::Char('k') => app.move_cursor(-1),
                KeyCode::Down | KeyCode::Char('j') => app.move_cursor(1),
                KeyCode::Char(' ') => app.toggle_current(),
                KeyCode::Char('a') => app.set_all(true),
                KeyCode::Char('x') => app.set_all(false),
                KeyCode::Enter | KeyCode::Char('c') => {
                    if app.approved().is_empty() {
                        app.status = Some("nothing approved -- space to toggle, a to approve all".into());
                    } else {
                        return Ok(Outcome::Convert);
                    }
                }
                _ => {}
            }
        }
    }
}

fn convert_approved(opts: &Options, app: &App) -> Result<()> {
    let paths: Vec<&PathBuf> = app.approved().into_iter().map(|c| &c.path).collect();
    println!("Converting {} approved file(s)...\n", paths.len());

    let status = Command::new(&opts.vidlib_bin)
        .arg("--db")
        .arg(&opts.db)
        .arg("convert")
        .args(paths)
        .args(&opts.convert_args)
        .arg("-y")
        .status()
        .with_context(|| format!("failed to run `{}`", opts.vidlib_bin))?;

    if !status.success() {
        anyhow::bail!("vidlib convert exited with {status}");
    }
    Ok(())
}

fn main() -> Result<()> {
    let opts = parse_args()?;

    if !opts.db.exists() {
        anyhow::bail!(
            "no vidlib database at {} -- run `vidlib scan <dir>` first",
            opts.db.display()
        );
    }

    let candidates = model::load_candidates(&opts.vidlib_bin, &opts.db, opts.under.as_deref())?;
    if candidates.is_empty() {
        println!("No --bloated candidates found. Run `vidlib scan <dir>` first, or relax the filters.");
        return Ok(());
    }

    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    let mut app = App::new(candidates);
    let outcome = run_app(&mut terminal, &mut app);

    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    drop(terminal);

    match outcome? {
        Outcome::Quit => {
            println!("No conversions started.");
            Ok(())
        }
        Outcome::Convert => convert_approved(&opts, &app),
    }
}
