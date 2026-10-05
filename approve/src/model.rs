// SPDX-License-Identifier: GPL-3.0-or-later
//! Reads vidlib's own SQLite library and the `vidlib` CLI's own `--bloated`
//! candidate list. All filtering and conversion logic stays in vidlib
//! (Python, already tested); this module only reads what it already computed.

use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{Context, Result, bail};
use rusqlite::Connection;
use serde::Deserialize;

/// The fields of vidlib's `VideoFile` we actually need to show. Every other
/// field in the stored JSON (there are many more) is ignored by serde_json.
#[derive(Debug, Deserialize)]
struct RawVideoFile {
    path: String,
    size: u64,
    probe_error: Option<String>,
    vcodec: Option<String>,
    width: Option<u32>,
    height: Option<u32>,
    #[serde(default)]
    sub_langs: Vec<String>,
    #[serde(default)]
    n_subs: u32,
    acodec: Option<String>,
    achannels: Option<u32>,
}

#[derive(Debug, Clone)]
pub struct Candidate {
    pub path: PathBuf,
    pub size: u64,
    pub resolution: String,
    pub encode: String,
    pub audio: String,
    pub subs: String,
    pub selected: bool,
}

impl Candidate {
    pub fn name(&self) -> String {
        self.path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_else(|| self.path.to_string_lossy().into_owned())
    }

    fn from_raw(raw: RawVideoFile) -> Self {
        let resolution = match (raw.width, raw.height) {
            (Some(w), Some(h)) => {
                let effective = h.max((w as f64 * 9.0 / 16.0).round() as u32);
                let label = if effective >= 1700 { "4K" } else { "" };
                if label.is_empty() {
                    format!("{w}x{h}")
                } else {
                    format!("{w}x{h} ({label})")
                }
            }
            _ => "?".to_string(),
        };
        let audio = match (raw.acodec.as_deref(), raw.achannels) {
            (Some(codec), Some(ch)) => {
                let layout = match ch {
                    1 => "mono".to_string(),
                    2 => "2.0".to_string(),
                    6 => "5.1".to_string(),
                    8 => "7.1".to_string(),
                    n => format!("{n}ch"),
                };
                format!("{codec} {layout}")
            }
            (Some(codec), None) => codec.to_string(),
            _ => "-".to_string(),
        };
        let subs = if raw.n_subs == 0 {
            "-".to_string()
        } else if raw.sub_langs.is_empty() {
            format!("{} (untagged)", raw.n_subs)
        } else {
            format!("{} ({})", raw.n_subs, raw.sub_langs.join(","))
        };
        Candidate {
            path: PathBuf::from(raw.path),
            size: raw.size,
            resolution,
            encode: raw.vcodec.unwrap_or_else(|| "?".to_string()),
            audio,
            subs,
            selected: true, // default to "approved"; the whole point is fast bulk review
        }
    }
}

/// Ask the already-tested Python tool which files it considers --bloated,
/// then pull each one's display fields straight out of vidlib's own cache.
/// vidlib's Filter logic is the single source of truth for "worth converting";
/// this binary never reimplements it.
pub fn load_candidates(vidlib_bin: &str, db_path: &Path, under: Option<&Path>) -> Result<Vec<Candidate>> {
    let mut cmd = Command::new(vidlib_bin);
    cmd.arg("--db").arg(db_path);
    cmd.arg("list").arg("--bloated").arg("--paths-only");
    if let Some(dir) = under {
        cmd.arg("--under").arg(dir);
    }
    let output = cmd
        .output()
        .with_context(|| format!("failed to run `{vidlib_bin}` -- is it on PATH?"))?;
    if !output.status.success() {
        bail!(
            "`{vidlib_bin} list --bloated` failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
    let paths: Vec<String> = String::from_utf8_lossy(&output.stdout)
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .map(str::to_string)
        .collect();
    if paths.is_empty() {
        return Ok(Vec::new());
    }

    let conn = Connection::open(db_path)
        .with_context(|| format!("cannot open vidlib database at {}", db_path.display()))?;
    let mut candidates = Vec::with_capacity(paths.len());
    let mut stmt = conn.prepare("SELECT data FROM files WHERE path = ?1")?;
    for path in &paths {
        let data: Option<String> = stmt
            .query_row([path], |row| row.get(0))
            .ok();
        let Some(data) = data else { continue };
        let raw: RawVideoFile = match serde_json::from_str(&data) {
            Ok(v) => v,
            Err(_) => continue,
        };
        if raw.probe_error.is_some() {
            continue;
        }
        candidates.push(Candidate::from_raw(raw));
    }
    // Biggest files first: the ones with the most to gain from a re-encode.
    candidates.sort_by_key(|c| std::cmp::Reverse(c.size));
    Ok(candidates)
}

pub fn default_db_path() -> PathBuf {
    let state = std::env::var_os("XDG_STATE_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| dirs::home_dir().unwrap_or_default().join(".local/state"));
    state.join("vidlib").join("library.db")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command as StdCommand;

    fn have_ffmpeg() -> bool {
        StdCommand::new("ffmpeg").arg("-version").output().is_ok()
    }

    /// `bin/vidlib` lives one directory up from this crate.
    fn vidlib_bin() -> String {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("bin/vidlib")
            .to_string_lossy()
            .into_owned()
    }

    #[test]
    fn candidate_formatting_from_raw_fields() {
        let raw = RawVideoFile {
            path: "/videos/Movie.mkv".into(),
            size: 123_456_789,
            probe_error: None,
            vcodec: Some("mpeg4".into()),
            width: Some(3840),
            height: Some(2160),
            sub_langs: vec!["eng".into()],
            n_subs: 1,
            acodec: Some("aac".into()),
            achannels: Some(6),
        };
        let c = Candidate::from_raw(raw);
        assert_eq!(c.name(), "Movie.mkv");
        assert!(c.resolution.contains("4K"));
        assert_eq!(c.audio, "aac 5.1");
        assert_eq!(c.subs, "1 (eng)");
        assert!(c.selected, "candidates default to approved");
    }

    #[test]
    fn load_candidates_round_trips_through_real_vidlib() {
        if !have_ffmpeg() {
            eprintln!("skipping: no ffmpeg on PATH");
            return;
        }
        let dir = std::env::temp_dir().join(format!("vidlib-approve-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let db = dir.join("library.db");

        // A deliberately bloated file: old codec at a high bitrate for its
        // resolution, well over the --bloated bits-per-pixel threshold.
        let bloated = dir.join("Bloated.mkv");
        StdCommand::new("ffmpeg")
            .args([
                "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=1",
                "-c:v", "mpeg4", "-b:v", "8M",
            ])
            .arg(&bloated)
            .status()
            .unwrap();

        // An efficient file that should NOT show up as a candidate.
        let efficient = dir.join("Efficient.mkv");
        StdCommand::new("ffmpeg")
            .args([
                "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=1",
                "-c:v", "libx265", "-preset", "ultrafast", "-crf", "30",
                "-x265-params", "log-level=error",
            ])
            .arg(&efficient)
            .status()
            .unwrap();

        let bin = vidlib_bin();
        let scan_status = StdCommand::new(&bin)
            .args(["--db"]).arg(&db)
            .args(["scan"]).arg(&dir)
            .args(["--min-size", "0"])
            .status()
            .expect("run vidlib scan");
        assert!(scan_status.success());

        let candidates = load_candidates(&bin, &db, None).expect("load_candidates");
        let names: Vec<_> = candidates.iter().map(|c| c.name()).collect();
        assert!(names.contains(&"Bloated.mkv".to_string()), "{names:?}");
        assert!(!names.contains(&"Efficient.mkv".to_string()), "{names:?}");

        std::fs::remove_dir_all(&dir).ok();
    }
}
