// SPDX-License-Identifier: GPL-3.0-or-later
//! Rendering only. All state lives in `App` (main.rs); this module just
//! draws it.

use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Cell, Paragraph, Row, Table, TableState};

use crate::App;

fn human_size(bytes: u64) -> String {
    let mut value = bytes as f64;
    for unit in ["B", "KB", "MB", "GB", "TB"] {
        if value < 1024.0 {
            return format!("{value:.1}{unit}");
        }
        value /= 1024.0;
    }
    format!("{value:.1}PB")
}

pub fn draw(frame: &mut Frame, app: &mut App) {
    let area = frame.area();
    let [header, body, footer] = Layout::vertical([
        Constraint::Length(2),
        Constraint::Min(3),
        Constraint::Length(3),
    ])
    .areas(area);

    draw_header(frame, header, app);
    draw_table(frame, body, app);
    draw_footer(frame, footer, app);
}

fn draw_header(frame: &mut Frame, area: Rect, app: &App) {
    let approved = app.candidates.iter().filter(|c| c.selected).count();
    let approved_size: u64 = app.candidates.iter().filter(|c| c.selected).map(|c| c.size).sum();
    let text = Line::from(vec![
        Span::styled(" vidlib approve ", Style::new().fg(Color::Black).bg(Color::Cyan)),
        Span::raw(format!(
            "  {} candidate(s)  ·  {} approved ({})",
            app.candidates.len(),
            approved,
            human_size(approved_size)
        )),
    ]);
    frame.render_widget(Paragraph::new(text), area);
}

fn draw_table(frame: &mut Frame, area: Rect, app: &mut App) {
    let rows = app.candidates.iter().map(|c| {
        let mark = if c.selected { "[x]" } else { "[ ]" };
        let style = if c.selected {
            Style::new().fg(Color::Green)
        } else {
            Style::new().fg(Color::DarkGray)
        };
        Row::new(vec![
            Cell::from(mark),
            Cell::from(c.name()),
            Cell::from(c.resolution.clone()),
            Cell::from(c.encode.clone()),
            Cell::from(c.audio.clone()),
            Cell::from(c.subs.clone()),
            Cell::from(human_size(c.size)),
        ])
        .style(style)
    });

    let widths = [
        Constraint::Length(3),
        Constraint::Min(20),
        Constraint::Length(14),
        Constraint::Length(8),
        Constraint::Length(12),
        Constraint::Length(14),
        Constraint::Length(10),
    ];

    let table = Table::new(rows, widths)
        .header(
            Row::new(vec!["", "Name", "Resolution", "Encode", "Audio", "Subs", "Size"])
                .style(Style::new().add_modifier(Modifier::BOLD)),
        )
        .block(Block::default().borders(Borders::ALL).title(" candidates (biggest first) "))
        .row_highlight_style(Style::new().add_modifier(Modifier::REVERSED))
        .highlight_symbol("> ");

    let mut state = TableState::default();
    state.select(Some(app.cursor));
    frame.render_stateful_widget(table, area, &mut state);
}

fn draw_footer(frame: &mut Frame, area: Rect, app: &App) {
    let keys = "↑/↓ move  ·  space toggle  ·  a approve all  ·  x clear all  ·  enter convert approved  ·  q quit";
    let mut lines = vec![Line::from(keys)];
    if let Some(status) = &app.status {
        lines.push(Line::from(Span::styled(
            status.clone(),
            Style::new().fg(Color::Yellow),
        )));
    }
    frame.render_widget(
        Paragraph::new(lines).block(Block::default().borders(Borders::TOP)),
        area,
    );
}
