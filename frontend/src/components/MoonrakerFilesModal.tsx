import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { File, Loader2, Play, RefreshCw, Trash2, X } from 'lucide-react';
import { api } from '../api/client';
import { Button } from './Button';
import { Card, CardContent } from './Card';
import { ConfirmModal } from './ConfirmModal';
import { useToast } from '../contexts/ToastContext';
import { formatFileSize } from '../utils/file';

interface MoonrakerFilesModalProps {
  printerId: number;
  printerName: string;
  /** The printer is mid-print: files can still be listed and deleted, but not started. */
  printerBusy: boolean;
  onClose: () => void;
}

/**
 * The G-code files stored on a Creality / Klipper printer (Moonraker's `gcodes` root):
 * start one, or delete it. The Bambu equivalent browses the SD card over FTP instead.
 */
export function MoonrakerFilesModal({ printerId, printerName, printerBusy, onClose }: MoonrakerFilesModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [toDelete, setToDelete] = useState<string | null>(null);

  const { data, isLoading, isError, isFetching, refetch } = useQuery({
    queryKey: ['moonrakerFiles', printerId],
    queryFn: () => api.getMoonrakerFiles(printerId),
  });

  const printMutation = useMutation({
    mutationFn: (path: string) => api.printMoonrakerFile(printerId, path),
    onSuccess: () => {
      showToast(t('printers.moonrakerFiles.printStarted'));
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printerId] });
      onClose();
    },
    onError: (error: Error) => showToast(error.message || t('printers.toast.failedToSendCommand'), 'error'),
  });

  const deleteMutation = useMutation({
    mutationFn: (path: string) => api.deleteMoonrakerFile(printerId, path),
    onSuccess: () => {
      showToast(t('printers.moonrakerFiles.deleted'));
      setToDelete(null);
      queryClient.invalidateQueries({ queryKey: ['moonrakerFiles', printerId] });
    },
    onError: (error: Error) => {
      setToDelete(null);
      showToast(error.message || t('printers.toast.failedToSendCommand'), 'error');
    },
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !toDelete) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, toDelete]);

  const files = data?.files ?? [];

  return (
    <div
      className="fixed inset-0 bg-black/50 flex items-start sm:items-center justify-center z-50 p-4 overflow-y-auto"
      onClick={onClose}
    >
      <Card
        className="w-full max-w-2xl my-auto max-h-[calc(100vh-2rem)] flex flex-col"
        onClick={(e: React.MouseEvent) => e.stopPropagation()}
      >
        <CardContent className="flex flex-col min-h-0">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-xl font-semibold truncate">
              {t('printers.browseFiles')} — {printerName}
            </h2>
            <div className="flex items-center gap-1">
              <Button
                variant="secondary"
                size="sm"
                onClick={() => refetch()}
                title={t('common.refresh')}
                aria-label={t('common.refresh')}
              >
                <RefreshCw className={`w-4 h-4 ${isFetching ? 'animate-spin' : ''}`} />
              </Button>
              <Button variant="secondary" size="sm" onClick={onClose} title={t('common.close')} aria-label={t('common.close')}>
                <X className="w-4 h-4" />
              </Button>
            </div>
          </div>

          {isLoading && (
            <div className="flex items-center justify-center gap-2 py-10 text-bambu-gray">
              <Loader2 className="w-5 h-5 animate-spin" />
              {t('common.loading')}
            </div>
          )}
          {isError && <p className="py-8 text-center text-sm text-red-500">{t('printers.moonrakerFiles.loadFailed')}</p>}
          {!isLoading && !isError && files.length === 0 && (
            <p className="py-10 text-center text-sm text-bambu-gray">{t('printers.moonrakerFiles.empty')}</p>
          )}

          {files.length > 0 && (
            <ul className="overflow-y-auto divide-y divide-bambu-dark-tertiary" aria-label={t('printers.browseFiles')}>
              {files.map((file) => (
                <li key={file.path} className="flex items-center gap-3 py-2">
                  <File className="w-4 h-4 text-bambu-gray shrink-0" />
                  <div className="min-w-0 flex-1">
                    <p className="text-sm text-white truncate" title={file.path}>
                      {file.path}
                    </p>
                    <p className="text-xs text-bambu-gray">
                      {formatFileSize(file.size)}
                      {file.modified > 0 && <> · {new Date(file.modified * 1000).toLocaleString()}</>}
                    </p>
                  </div>
                  <Button
                    size="sm"
                    onClick={() => printMutation.mutate(file.path)}
                    disabled={printerBusy || printMutation.isPending}
                    title={printerBusy ? t('printers.moonrakerFiles.busy') : t('common.print')}
                    aria-label={`${t('common.print')} ${file.path}`}
                  >
                    <Play className="w-4 h-4" />
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => setToDelete(file.path)}
                    title={t('common.delete')}
                    aria-label={`${t('common.delete')} ${file.path}`}
                  >
                    <Trash2 className="w-4 h-4" />
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      {toDelete && (
        <ConfirmModal
          title={t('fileManager.deleteFile')}
          message={t('printerFiles.deleteFileConfirm', { name: toDelete.split('/').pop() })}
          confirmText={t('common.delete')}
          variant="danger"
          isLoading={deleteMutation.isPending}
          onConfirm={() => deleteMutation.mutate(toDelete)}
          onCancel={() => setToDelete(null)}
        />
      )}
    </div>
  );
}
