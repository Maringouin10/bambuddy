/**
 * Tests for adding an Anycubic (LAN Mode) printer: only a name and an IP are
 * asked for, the Bambu pre-flight diagnostic is skipped, and the backend's
 * refusal reasons (LAN Mode off, unsupported model) get their own message.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const bambuPrinter = {
  id: 1,
  name: 'X1 Carbon',
  ip_address: '192.168.1.100',
  serial_number: '00M09A350100001',
  access_code: '12345678',
  printer_type: 'bambu',
  model: 'X1C',
  location: null,
  auto_archive: true,
  is_active: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const status = {
  connected: true,
  state: 'IDLE',
  progress: 0,
  layer_num: 0,
  total_layers: 0,
  temperatures: { nozzle: 25, bed: 25 },
  remaining_time: 0,
  filename: null,
  vt_tray: [],
};

let created: Record<string, unknown> | null;
let diagnosticCalls: number;
let createReply: () => Response;

describe('Anycubic printers', () => {
  beforeEach(() => {
    created = null;
    diagnosticCalls = 0;
    createReply = () =>
      HttpResponse.json({ ...bambuPrinter, id: 3, printer_type: 'anycubic', model: 'Kobra 3 Max' });
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([bambuPrinter])),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(status)),
      http.get('/api/v1/queue/', () => HttpResponse.json([])),
      http.get('/api/v1/discovery/info', () =>
        HttpResponse.json({ is_docker: false, ssdp_running: false, scan_running: false, subnets: [] }),
      ),
      http.post('/api/v1/printers/diagnostic', () => {
        diagnosticCalls += 1;
        return HttpResponse.json({ checks: [] });
      }),
      http.post('/api/v1/printers/', async ({ request }) => {
        created = (await request.json()) as Record<string, unknown>;
        return createReply();
      }),
    );
  });

  function submitButton() {
    return screen.getAllByRole('button', { name: /^add printer$/i }).at(-1)!;
  }

  async function openAnycubic() {
    render(<PrintersPage />);
    await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());
    await userEvent.click(screen.getByText(/add printer/i));
    await userEvent.click(await screen.findByRole('radio', { name: 'Anycubic' }));
  }

  it('asks only for a name and an IP, and offers the Kobra 3 Max', async () => {
    await openAnycubic();
    expect(screen.getByRole('radio', { name: 'Anycubic' })).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByText(/LAN Mode/)).toBeInTheDocument();
    expect(screen.queryByPlaceholderText('01P00A000000000')).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText('7125')).not.toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Kobra 3 Max' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'X1 Carbon' })).not.toBeInTheDocument();
  });

  it('submits without serial or access code and skips the Bambu diagnostic', async () => {
    await openAnycubic();
    await userEvent.type(screen.getByPlaceholderText(/my printer/i), 'Kobra');
    await userEvent.type(screen.getByPlaceholderText(/192\.168\.1\.100/), '192.168.1.70');
    await userEvent.click(submitButton());

    await waitFor(() => expect(created).not.toBeNull());
    expect(created).toMatchObject({
      name: 'Kobra',
      ip_address: '192.168.1.70',
      printer_type: 'anycubic',
      model: 'Kobra 3 Max',
      serial_number: '',
      access_code: '',
    });
    expect(diagnosticCalls).toBe(0);
  });

  it('explains that LAN Mode is off', async () => {
    createReply = () =>
      HttpResponse.json(
        { detail: { code: 'printer_connection_failed', reason: 'lan_mode_off', message: 'off' } },
        { status: 400 },
      );
    await openAnycubic();
    await userEvent.type(screen.getByPlaceholderText(/my printer/i), 'Kobra');
    await userEvent.type(screen.getByPlaceholderText(/192\.168\.1\.100/), '192.168.1.71');
    await userEvent.click(submitButton());
    expect(await screen.findByText(/LAN Mode is off on this printer/)).toBeInTheDocument();
  });
});
