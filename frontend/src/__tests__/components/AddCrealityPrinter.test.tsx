/**
 * Tests for adding / editing a Creality (Klipper / Moonraker) printer.
 *
 * A Creality printer has no serial number, access code or network discovery:
 * the form asks for a name, an IP and (optionally) the Moonraker port, and
 * the Bambu MQTT/FTPS pre-flight diagnostic is skipped.
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

const crealityPrinter = {
  ...bambuPrinter,
  id: 2,
  name: 'Ender 3 V3 KE',
  ip_address: '192.168.1.50',
  serial_number: 'CREALITY-192.168.1.50',
  access_code: '',
  printer_type: 'creality',
  api_port: 4408,
  model: 'Ender-3 V3 KE',
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
let updated: Record<string, unknown> | null;

describe('Creality printers', () => {
  beforeEach(() => {
    created = null;
    updated = null;
    diagnosticCalls = 0;
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([bambuPrinter, crealityPrinter])),
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
        return HttpResponse.json({ ...crealityPrinter, id: 3 });
      }),
      http.patch('/api/v1/printers/:id', async ({ request }) => {
        updated = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(crealityPrinter);
      }),
    );
  });

  // The page's own "Add Printer" button stays mounted behind the modal; the
  // modal's submit button is the last one in the DOM.
  function submitButton() {
    return screen.getAllByRole('button', { name: /^add printer$/i }).at(-1)!;
  }

  async function openAddModal() {
    render(<PrintersPage />);
    await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());
    await userEvent.click(screen.getByText(/add printer/i));
    await waitFor(() => expect(screen.getByRole('radio', { name: 'Creality' })).toBeInTheDocument());
  }

  it('defaults to Bambu Lab with serial number and access code fields', async () => {
    await openAddModal();

    expect(screen.getByRole('radio', { name: 'Bambu Lab' })).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByRole('radio', { name: 'Creality' })).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByPlaceholderText('01P00A000000000')).toBeInTheDocument();
  });

  it('hides Bambu-only fields and shows the Moonraker port for Creality', async () => {
    await openAddModal();
    await userEvent.click(screen.getByRole('radio', { name: 'Creality' }));

    expect(screen.getByRole('radio', { name: 'Creality' })).toHaveAttribute('aria-checked', 'true');
    expect(screen.queryByPlaceholderText('01P00A000000000')).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/from printer settings/i)).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText('7125')).toBeInTheDocument();
    // Creality models are offered instead of Bambu ones.
    expect(screen.getByRole('option', { name: 'Ender-3 V3 KE' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'K2 Plus' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'X1 Carbon' })).not.toBeInTheDocument();
  });

  it('submits a Creality printer without serial number or access code and skips the Bambu diagnostic', async () => {
    await openAddModal();
    await userEvent.click(screen.getByRole('radio', { name: 'Creality' }));

    await userEvent.type(screen.getByPlaceholderText(/my printer/i), 'Ender 3 V3');
    await userEvent.type(screen.getByPlaceholderText(/192\.168\.1\.100/), '192.168.1.60');
    await userEvent.type(screen.getByPlaceholderText('7125'), '4408');

    await userEvent.click(submitButton());

    await waitFor(() => expect(created).not.toBeNull());
    expect(created).toMatchObject({
      name: 'Ender 3 V3',
      ip_address: '192.168.1.60',
      printer_type: 'creality',
      api_port: 4408,
      serial_number: '',
      access_code: '',
    });
    expect(diagnosticCalls).toBe(0);
  });

  it('leaves the port out when it is blank so the backend uses 7125', async () => {
    await openAddModal();
    await userEvent.click(screen.getByRole('radio', { name: 'Creality' }));
    await userEvent.type(screen.getByPlaceholderText(/my printer/i), 'Ender');
    await userEvent.type(screen.getByPlaceholderText(/192\.168\.1\.100/), '192.168.1.61');
    await userEvent.click(submitButton());

    await waitFor(() => expect(created).not.toBeNull());
    expect(created).not.toHaveProperty('api_port');
  });

  it('edits a Creality printer: port prefilled, no serial/access code, Bambu diagnostic skipped', async () => {
    server.use(http.get('/api/v1/printers/', () => HttpResponse.json([crealityPrinter])));
    render(<PrintersPage />);
    await waitFor(() => expect(screen.getByText('Ender 3 V3 KE')).toBeInTheDocument());

    const menuBtn = [...document.querySelectorAll('button')].find((b) =>
      b.querySelector('.lucide-ellipsis-vertical'),
    )!;
    await userEvent.click(menuBtn);
    await userEvent.click(await screen.findByRole('button', { name: /^edit$/i }));
    await screen.findByText('Edit Printer');

    const port = screen.getByPlaceholderText('7125');
    expect(port).toHaveValue(4408);
    expect(screen.queryByDisplayValue('CREALITY-192.168.1.50')).not.toBeInTheDocument();

    await userEvent.clear(port);
    await userEvent.type(port, '7126');
    await userEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(updated).not.toBeNull());
    expect(updated).toMatchObject({ ip_address: '192.168.1.50', api_port: 7126 });
    expect(updated).not.toHaveProperty('access_code');
    expect(diagnosticCalls).toBe(0);
  });

  it('switching back to Bambu restores the credential fields', async () => {
    await openAddModal();
    await userEvent.click(screen.getByRole('radio', { name: 'Creality' }));
    await userEvent.click(screen.getByRole('radio', { name: 'Bambu Lab' }));

    expect(screen.getByPlaceholderText('01P00A000000000')).toBeInTheDocument();
    expect(screen.queryByPlaceholderText('7125')).not.toBeInTheDocument();
  });
});
