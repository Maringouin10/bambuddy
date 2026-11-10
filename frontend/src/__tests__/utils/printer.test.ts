/**
 * Tests for getPrinterImage — model → printer card image resolver.
 *
 * X2D support (#988): both the display name "X2D" and the internal SSDP
 * code "N6" must resolve to /img/printers/x2d.png so the Printers page
 * and PrinterInfoModal show the correct artwork instead of falling back
 * to default.png.
 */

import { describe, it, expect } from 'vitest';
import {
  getPrinterImage,
  isGcodeCompatible,
  isCrealityModel,
  isModelMatchForPrinter,
  filterCompatibleQueueItems,
} from '../../utils/printer';
import { getAmsLabel } from '../../utils/amsHelpers';
import type { PrintQueueItem } from '../../api/client';

describe('getPrinterImage', () => {
  describe('Creality (Klipper)', () => {
    it('uses the Ender-3 V3 picture for every Creality model', () => {
      for (const model of ['Ender-3 V3', 'Ender-3 V3 KE', 'Ender-3 V3 Plus', 'ender3v3plus']) {
        expect(getPrinterImage(model)).toBe('/img/printers/ender3v3.png');
      }
    });

    it('uses it for a Creality printer whose model was left blank', () => {
      expect(getPrinterImage(null, 'creality')).toBe('/img/printers/ender3v3.png');
      expect(getPrinterImage('', 'creality')).toBe('/img/printers/ender3v3.png');
    });

    it('gives the K2 series its own picture', () => {
      expect(getPrinterImage('K2 Plus', 'creality')).toBe('/img/printers/k2plus.png');
      expect(getPrinterImage('K2 Pro')).toBe('/img/printers/k2plus.png');
    });

    it('leaves Bambu printers alone', () => {
      expect(getPrinterImage('X1C', 'bambu')).toBe('/img/printers/x1c.png');
      expect(getPrinterImage(null)).toBe('/img/printers/default.png');
    });
  });

  describe('X2D (#988)', () => {
    it('resolves display name "X2D" to x2d.png', () => {
      expect(getPrinterImage('X2D')).toBe('/img/printers/x2d.png');
    });

    it('resolves case-insensitive variants', () => {
      expect(getPrinterImage('x2d')).toBe('/img/printers/x2d.png');
      expect(getPrinterImage(' X2D ')).toBe('/img/printers/x2d.png');
    });

    it('resolves the internal SSDP code "N6" to x2d.png', () => {
      expect(getPrinterImage('N6')).toBe('/img/printers/x2d.png');
    });

    it('does not match X2D on unrelated model strings', () => {
      // Regression guard: a hypothetical future "X2" model must not
      // silently pick up x2d.png until it's explicitly mapped.
      expect(getPrinterImage('X2E')).toBe('/img/printers/default.png');
    });
  });

  describe('A2L (#1684)', () => {
    it('resolves display name "A2L" to a2l.png', () => {
      expect(getPrinterImage('A2L')).toBe('/img/printers/a2l.png');
    });

    it('resolves case-insensitive variants', () => {
      expect(getPrinterImage('a2l')).toBe('/img/printers/a2l.png');
      expect(getPrinterImage(' A2L ')).toBe('/img/printers/a2l.png');
    });

    it('resolves the internal SSDP code "N9" to a2l.png', () => {
      expect(getPrinterImage('N9')).toBe('/img/printers/a2l.png');
    });

    it('does not match A2L on unrelated A-series strings', () => {
      // Regression guard: a hypothetical future "A2M" or similar must not
      // silently pick up a2l.png until it's explicitly mapped, and "A1" /
      // "A1 Mini" must still resolve to their own artwork.
      expect(getPrinterImage('A2M')).toBe('/img/printers/default.png');
      expect(getPrinterImage('A1')).toBe('/img/printers/a1.png');
      expect(getPrinterImage('A1 Mini')).toBe('/img/printers/a1mini.png');
    });
  });

  describe('regression: existing families unchanged', () => {
    it('X1C → x1c.png', () => {
      expect(getPrinterImage('X1C')).toBe('/img/printers/x1c.png');
    });

    it('X1E → x1e.png', () => {
      expect(getPrinterImage('X1E')).toBe('/img/printers/x1e.png');
    });

    it('H2D → h2d.png', () => {
      expect(getPrinterImage('H2D')).toBe('/img/printers/h2d.png');
    });

    it('H2D Pro → h2dpro.png', () => {
      expect(getPrinterImage('H2D Pro')).toBe('/img/printers/h2dpro.png');
    });

    it('P2S → p1s.png (shared with P1S)', () => {
      // Pre-existing behaviour: P2S currently reuses the P1S artwork. Not
      // changed by the X2D diff; asserted to catch accidental regressions.
      expect(getPrinterImage('P2S')).toBe('/img/printers/p1s.png');
    });

    it('A1 Mini → a1mini.png (not a1.png)', () => {
      // The "a1mini" branch must run before the generic "a1" branch —
      // the X2D branch was inserted above both and must not break order.
      expect(getPrinterImage('A1 Mini')).toBe('/img/printers/a1mini.png');
    });

    it('null / undefined → default.png', () => {
      expect(getPrinterImage(null)).toBe('/img/printers/default.png');
      expect(getPrinterImage(undefined)).toBe('/img/printers/default.png');
    });

    it('unknown model → default.png', () => {
      expect(getPrinterImage('SomeFuturePrinter')).toBe(
        '/img/printers/default.png',
      );
    });
  });
});

// Mirrors backend/tests/unit/test_scheduler_model_mismatch.py — the frontend
// table must stay in sync with backend GCODE_COMPAT_FAMILIES (#2578).
describe('isGcodeCompatible', () => {
  it('accepts same model', () => {
    expect(isGcodeCompatible('X1C', 'X1C')).toBe(true);
    expect(isGcodeCompatible('H2D', 'H2D')).toBe(true);
  });

  it('accepts the X1/P1 interchange family', () => {
    expect(isGcodeCompatible('X1C', 'P1S')).toBe(true);
    expect(isGcodeCompatible('X1C', 'P1P')).toBe(true);
    expect(isGcodeCompatible('P1S', 'X1E')).toBe(true);
    expect(isGcodeCompatible('X1', 'P1P')).toBe(true);
  });

  it('rejects cross-family targets', () => {
    // The reporter's case: X1C-sliced G-code targeted at an H2D (#2578)
    expect(isGcodeCompatible('X1C', 'H2D')).toBe(false);
    expect(isGcodeCompatible('P1S', 'H2D')).toBe(false);
    expect(isGcodeCompatible('X1C', 'A1')).toBe(false);
    expect(isGcodeCompatible('A1', 'A1 Mini')).toBe(false);
    expect(isGcodeCompatible('H2D', 'H2S')).toBe(false);
    expect(isGcodeCompatible('X1C', 'P2S')).toBe(false);
  });

  it('is fail-safe on unknown metadata', () => {
    expect(isGcodeCompatible(null, 'H2D')).toBe(true);
    expect(isGcodeCompatible('X1C', null)).toBe(true);
    expect(isGcodeCompatible(undefined, undefined)).toBe(true);
    expect(isGcodeCompatible('', 'H2D')).toBe(true);
  });

  it('normalizes case, spaces and dashes', () => {
    expect(isGcodeCompatible('x1c', 'X1C')).toBe(true);
    expect(isGcodeCompatible('A1 Mini', 'A1-MINI')).toBe(true);
    expect(isGcodeCompatible('H2D Pro', 'H2DPRO')).toBe(true);
  });
});

describe('filterCompatibleQueueItems — force-color PLA variant (#2650)', () => {
  const makeItem = (
    overrides: Array<{ slot_id: number; type: string; color: string; tray_info_idx?: string; force_color_match?: boolean }>,
  ): PrintQueueItem => ({ id: 1, filament_overrides: overrides } as unknown as PrintQueueItem);

  // A job sliced for White PLA Matte (GFA01).
  const matteJob = makeItem([
    { slot_id: 1, type: 'PLA', color: '#FFFFFF', tray_info_idx: 'GFA01', force_color_match: true },
  ]);
  const loadedTypes = new Set(['PLA']);
  const loaded = new Set(['PLA:ffffff']);

  it('rejects a printer loaded only with other white PLA variants (Basic/Silk)', () => {
    const variants = new Set(['PLA:ffffff:GFA00', 'PLA:ffffff:GFA06']);
    expect(filterCompatibleQueueItems([matteJob], loadedTypes, loaded, variants)).toHaveLength(0);
  });

  it('accepts a printer loaded with the matching variant (Matte GFA01)', () => {
    const variants = new Set(['PLA:ffffff:GFA00', 'PLA:ffffff:GFA01']);
    expect(filterCompatibleQueueItems([matteJob], loadedTypes, loaded, variants)).toHaveLength(1);
  });

  it('accepts a same-colour spool that reports no tray_info_idx (custom/third-party)', () => {
    const variants = new Set(['PLA:ffffff:']);
    expect(filterCompatibleQueueItems([matteJob], loadedTypes, loaded, variants)).toHaveLength(1);
  });

  it('falls back to type+colour when no variant data is supplied', () => {
    // loadedVariants omitted → the hint is never stricter than the data it has.
    expect(filterCompatibleQueueItems([matteJob], loadedTypes, loaded)).toHaveLength(1);
  });

  it('an override without a tray_info_idx keeps the old type+colour behaviour', () => {
    const noIdxJob = makeItem([{ slot_id: 1, type: 'PLA', color: '#FFFFFF', force_color_match: true }]);
    const variants = new Set(['PLA:ffffff:GFA06']);
    expect(filterCompatibleQueueItems([noIdxJob], loadedTypes, loaded, variants)).toHaveLength(1);
  });
});

describe('filterCompatibleQueueItems — printer-targeted jobs (#3133)', () => {
  // A job moved from "Any P2S" to one P2S keeps its override. The printer is
  // already chosen, so a colour it has not loaded must not hide the job from
  // that printer's card — the scheduler maps the trays at dispatch.
  const boneWhite = [{ slot_id: 1, type: 'PLA', color: '#F5F5DC', force_color_match: false }];
  const loadedTypes = new Set(['PLA']);
  const loadedBrown = new Set(['PLA:8b4513']);

  it('keeps a specific-printer job whose override colour is not loaded', () => {
    const item = { id: 1, printer_id: 3, filament_overrides: boneWhite } as unknown as PrintQueueItem;
    expect(filterCompatibleQueueItems([item], loadedTypes, loadedBrown)).toHaveLength(1);
  });

  it('still filters the same job while it targets any printer of a model', () => {
    const item = { id: 1, printer_id: null, target_model: 'P2S', filament_overrides: boneWhite } as unknown as PrintQueueItem;
    expect(filterCompatibleQueueItems([item], loadedTypes, loadedBrown)).toHaveLength(0);
  });
});


describe('Creality model matching', () => {
  it('recognises Creality machine names', () => {
    for (const m of ['Ender-3 V3 KE', 'Creality Ender-3 V3', 'K2 Plus', 'K2 Pro', 'k1c', 'ENDER-3 V3 PLUS']) {
      expect(isCrealityModel(m)).toBe(true);
    }
    for (const m of ['P1S', 'X1C', 'A1 Mini', 'H2D', 'K', '', null, undefined]) {
      expect(isCrealityModel(m)).toBe(false);
    }
  });

  it('accepts any Creality G-code on any Creality printer', () => {
    // The regression: a file sliced for "Creality Ender-3 V3" was refused on a printer saved as
    // "Ender-3 V3 Plus", and the Ender was not even offered as a target.
    expect(isModelMatchForPrinter('Creality Ender-3 V3', 'Ender-3 V3 Plus')).toBe(true);
    expect(isModelMatchForPrinter('Creality Ender-3 V3', 'K2 Plus')).toBe(true);
    expect(isGcodeCompatible('Creality Ender-3 V3', 'Ender-3 V3 KE')).toBe(true);
  });

  it('still refuses to mix Bambu and Creality', () => {
    expect(isModelMatchForPrinter('Creality Ender-3 V3', 'P1S')).toBe(false);
    expect(isModelMatchForPrinter('P1S', 'Ender-3 V3 Plus')).toBe(false);
    expect(isGcodeCompatible('X1C', 'K2 Plus')).toBe(false);
  });

  it('keeps the Bambu rules: exact model (case-insensitive) and unknown passes', () => {
    expect(isModelMatchForPrinter('P1S', 'p1s')).toBe(true);
    expect(isModelMatchForPrinter('P1S', 'X1C')).toBe(false);
    expect(isModelMatchForPrinter(null, 'X1C')).toBe(true);
    expect(isModelMatchForPrinter('P1S', undefined)).toBe(true);
  });
});


describe('getAmsLabel for the Creality CFS', () => {
  it('calls a CFS unit CFS, and leaves Bambu units alone', () => {
    expect(getAmsLabel(0, 4, 'cfs')).toBe('CFS-A');
    expect(getAmsLabel(1, 4, 'cfs')).toBe('CFS-B');
    expect(getAmsLabel(0, 4)).toBe('AMS-A');
    expect(getAmsLabel(0, 4, 'ams')).toBe('AMS-A');
    expect(getAmsLabel(128, 1)).toBe('HT-A');
  });
});

describe('Anycubic models', () => {
  it('recognises Kobra names and pairs slicer profile with printer', async () => {
    const { isAnycubicModel, isGcodeCompatible, isModelMatchForPrinter, getPrinterImage } = await import(
      '../../utils/printer'
    );
    expect(isAnycubicModel('Kobra 3 Max')).toBe(true);
    expect(isAnycubicModel('Anycubic Kobra 3 Max')).toBe(true);
    expect(isAnycubicModel('K2 Plus')).toBe(false);
    expect(isGcodeCompatible('Anycubic Kobra 3 Max', 'Kobra 3 Max')).toBe(true);
    expect(isGcodeCompatible('Anycubic Kobra 3', 'Kobra 3 Max')).toBe(false);
    expect(isModelMatchForPrinter('Anycubic Kobra 3 Max', 'Kobra 3 Max')).toBe(true);
    expect(isModelMatchForPrinter('Anycubic Kobra 3 Max', 'P1S')).toBe(false);
    expect(getPrinterImage('Kobra 3 Max', 'anycubic')).toBe('/img/printers/kobra3max.png');
  });
});
