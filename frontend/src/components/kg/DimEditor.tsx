/** 维度编辑器。 */
import { useState } from 'react'
import { api, ApiError } from '@/api/client'
import { Field, Modal } from '@/components/common/ui'
import { useI18n } from '@/i18n'
import { toast } from '@/stores/app'
import type { Dimension } from '@/types'

const DIM_TYPES = ['time', 'geo', 'channel', 'product', 'user_segment', 'dept', 'other']

export function DimEditor({ dim, onClose, onSaved }: { dim: Dimension; onClose: () => void; onSaved: () => void }) {
  const { t } = useI18n()
  const [f, setF] = useState({
    name: dim.name,
    aliases: (dim.aliases || []).join(','),
    description: dim.description || '',
    dimension_type: dim.dimension_type,
    values: (dim.value_examples || []).join(','),
  })
  const [busy, setBusy] = useState(false)

  const save = async () => {
    setBusy(true)
    try {
      await api.updateDimension(dim.id, {
        name: f.name.trim(),
        aliases: f.aliases.split(',').map((s) => s.trim()).filter(Boolean),
        description: f.description,
        dimension_type: f.dimension_type,
        value_examples: f.values.split(',').map((s) => s.trim()).filter(Boolean),
      })
      toast.ok(t('kg.dimSaved'))
      onSaved()
    } catch (e) {
      toast.error((e as ApiError).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title={`${t('kg.editDim')} · ${dim.name}`} width={560} onClose={onClose}
      footer={<>
        <button className="btn" onClick={onClose}>{t('common.cancel')}</button>
        <button className="btn primary" disabled={busy} onClick={() => void save()}>{t('common.save')}</button>
      </>}>
      <div className="col" style={{ gap: 13 }}>
        <div className="grid2">
          <Field label={`${t('kg.dimName')} *`}>
            <input className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
          </Field>
          <Field label={t('kg.dimType')}>
            <select className="select" value={f.dimension_type}
              onChange={(e) => setF({ ...f, dimension_type: e.target.value })}>
              {DIM_TYPES.map((dt) => <option key={dt} value={dt}>{dt}</option>)}
            </select>
          </Field>
        </div>
        <Field label={t('kg.aliasesLabel')}>
          <input className="input" value={f.aliases} onChange={(e) => setF({ ...f, aliases: e.target.value })} />
        </Field>
        <Field label={t('common.description')}>
          <textarea className="textarea" rows={2} value={f.description}
            onChange={(e) => setF({ ...f, description: e.target.value })} />
        </Field>
        <Field label={t('kg.valuesLabel')} hint={t('kg.valuesHint')}>
          <input className="input" value={f.values} onChange={(e) => setF({ ...f, values: e.target.value })} />
        </Field>
      </div>
    </Modal>
  )
}
