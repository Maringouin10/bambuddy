/**
 * The file browser of a Creality / Klipper printer: its G-code files, start one, delete one.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { MoonrakerFilesModal } from '../../components/MoonrakerFilesModal';

const FILES = [
  { path: 'sub/Benchy.gcode', size: 2048, modified: 1_700_000_300 },
  { path: 'Cube.gcode', size: 10, modified: 1_700_000_100 },
];

let printed: string[];
let deleted: string[];

function mockPrinter({ failDelete = false } = {}) {
  server.use(
    http.get('/api/v1/printers/1/moonraker/files', () => HttpResponse.json({ files: FILES, total: 2 })),
    http.post('/api/v1/printers/1/moonraker/files/print', ({ request }) => {
      printed.push(new URL(request.url).searchParams.get('path') ?? '');
      return HttpResponse.json({ success: true, message: 'ok' });
    }),
    http.delete('/api/v1/printers/1/moonraker/files', ({ request }) => {
      if (failDelete) return HttpResponse.json({ detail: 'It is being printed' }, { status: 500 });
      deleted.push(new URL(request.url).searchParams.get('path') ?? '');
      return HttpResponse.json({ success: true, message: 'ok' });
    }),
  );
}

function open(printerBusy = false, onClose = () => {}) {
  render(<MoonrakerFilesModal printerId={1} printerName="My Ender" printerBusy={printerBusy} onClose={onClose} />);
}

describe('MoonrakerFilesModal', () => {
  beforeEach(() => {
    printed = [];
    deleted = [];
  });

  it("lists the printer's files with their size", async () => {
    mockPrinter();
    open();

    expect(await screen.findByText('sub/Benchy.gcode')).toBeInTheDocument();
    expect(screen.getByText('Cube.gcode')).toBeInTheDocument();
    expect(screen.getByText(/2\.0 KB/)).toBeInTheDocument();
    expect(screen.getByText(/My Ender/)).toBeInTheDocument();
  });

  it('starts a file on an idle printer and closes', async () => {
    mockPrinter();
    const onClose = vi.fn();
    open(false, onClose);

    await userEvent.click(await screen.findByRole('button', { name: /print sub\/Benchy\.gcode/i }));

    await waitFor(() => expect(printed).toEqual(['sub/Benchy.gcode']));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('cannot start a file while the printer is busy', async () => {
    mockPrinter();
    open(true);

    expect(await screen.findByRole('button', { name: /print cube\.gcode/i })).toBeDisabled();
    // but deleting is still possible
    expect(screen.getByRole('button', { name: /delete cube\.gcode/i })).toBeEnabled();
  });

  it('asks before deleting, then deletes', async () => {
    mockPrinter();
    open();

    await userEvent.click(await screen.findByRole('button', { name: /delete cube\.gcode/i }));
    expect(deleted).toEqual([]); // nothing yet: a confirmation is showing
    const confirm = (await screen.findAllByRole('button', { name: /^delete$/i })).at(-1)!;
    await userEvent.click(confirm);

    await waitFor(() => expect(deleted).toEqual(['Cube.gcode']));
  });

  it('shows an empty state', async () => {
    server.use(http.get('/api/v1/printers/1/moonraker/files', () => HttpResponse.json({ files: [], total: 0 })));
    open();
    expect(await screen.findByText(/no g-code files on the printer/i)).toBeInTheDocument();
  });

  it('says so when the list cannot be loaded', async () => {
    server.use(http.get('/api/v1/printers/1/moonraker/files', () => HttpResponse.json({ detail: 'x' }, { status: 400 })));
    open();
    expect(await screen.findByText(/could not load the printer's files/i)).toBeInTheDocument();
  });
});
