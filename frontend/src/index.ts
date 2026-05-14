// PocketHunter Mol* bridge — exposes a small API on top of Mol*'s full
// internals so the Streamlit component can drive pocket surfaces,
// cluster overpaints, ligand pose loading, camera focus, and residue
// click round-trips. The default Mol* viewer-bundle UMD only exposes
// the ``Viewer`` class itself; everything we need below is bundled but
// private. By importing directly from ``molstar/lib/...`` we get the
// full API at build time and expose only what we need at runtime via
// ``window.molstarBridge``.

import { Viewer } from "molstar/lib/apps/viewer/app";
import { MolScriptBuilder as MS } from "molstar/lib/mol-script/language/builder";
import { Color } from "molstar/lib/mol-util/color";
import { StateTransforms } from "molstar/lib/mol-plugin-state/transforms";
import { StructureSelection } from "molstar/lib/mol-model/structure/query";
import { StructureSelectionQueries } from "molstar/lib/mol-plugin-state/helpers/structure-selection-queries";
import { StateObjectRef } from "molstar/lib/mol-state";
import { compile } from "molstar/lib/mol-script/runtime/query/compiler";
import { QueryContext } from "molstar/lib/mol-model/structure/query";
import "molstar/build/viewer/molstar.css";

type Hex = string;

export interface PocketAnno {
  residues: string[]; // e.g. ["A_125", "A_147"]
  color: Hex;
  label: string;
}

export interface ClusterAnno {
  cluster_id: number;
  residues: string[];
  color: Hex;
}

export interface LigandPoseAnno {
  sdf: string;
  color: Hex;
  label?: string;
}

function hexToColor(hex: Hex): number {
  const m = hex.replace("#", "");
  return parseInt(m, 16);
}

// Compose a MolScript Expression matching a residue list of the form
// ["A_125", "A_147", "B_203"]. Groups residues by chain so the expression
// stays compact even for many residues.
function residuesExpression(residues: string[]) {
  const byChain = new Map<string, number[]>();
  for (const r of residues) {
    const idx = r.indexOf("_");
    if (idx < 0) continue;
    const chain = r.slice(0, idx);
    const num = parseInt(r.slice(idx + 1), 10);
    if (!isFinite(num) || !chain) continue;
    if (!byChain.has(chain)) byChain.set(chain, []);
    byChain.get(chain)!.push(num);
  }
  if (byChain.size === 0) return null;

  const groups: any[] = [];
  for (const [chain, nums] of byChain) {
    groups.push(
      MS.struct.generator.atomGroups({
        "chain-test": MS.core.rel.eq([
          MS.struct.atomProperty.macromolecular.label_asym_id(),
          chain,
        ]),
        "residue-test": MS.core.set.has([
          MS.set(...nums),
          MS.struct.atomProperty.macromolecular.auth_seq_id(),
        ]),
        "group-by": MS.struct.atomProperty.macromolecular.residueKey(),
      })
    );
  }
  return groups.length === 1 ? groups[0] : MS.struct.modifier.union(groups);
}

export class MolViewer {
  private viewer: Viewer;
  private plugin: any;
  private pocketRefs: string[] = [];
  private clusterRefs: string[] = [];
  private ligandPoseRef: string | null = null;
  private clickUnsubscribe: (() => void) | null = null;

  private constructor(viewer: Viewer) {
    this.viewer = viewer;
    this.plugin = (viewer as any).plugin;
  }

  static async create(root: HTMLElement, options: any = {}): Promise<MolViewer> {
    const viewer = await Viewer.create(root, options);
    return new MolViewer(viewer);
  }

  async loadStructure(url: string, format: string): Promise<void> {
    await this.viewer.loadStructureFromUrl(url, format as any);
  }

  // Update the visible model index without re-loading the whole
  // structure. ``modelIndex`` is 0-based; the Streamlit-side slider is
  // 1-based and converts before calling.
  async setCurrentModel(modelIndex: number): Promise<void> {
    const traj = this.plugin.managers.structure.hierarchy.current.trajectories[0];
    if (!traj || !traj.models || traj.models.length === 0) return;
    const modelCell = traj.models[0].cell;
    const update = this.plugin.state.data.build();
    update.to(modelCell.transform.ref).update({ modelIndex });
    await update.commit();
  }

  // Add a molecular-surface representation scoped to the pocket's
  // residues. Each pocket gets its own state ref so we can tear it
  // down individually.
  async showPocketSurface(pocket: PocketAnno): Promise<void> {
    const structure = this._firstStructure();
    if (!structure) return;
    const expression = residuesExpression(pocket.residues);
    if (!expression) return;

    try {
      // ``StructureSelectionFromExpression`` produces a sub-structure
      // limited to atoms matching the MolScript expression; that
      // sub-structure is what we attach the surface representation to.
      const builder = this.plugin.state.data.build();
      const sel = builder
        .to(structure.cell)
        .apply(
          StateTransforms.Model.StructureSelectionFromExpression,
          { expression, label: pocket.label || "Pocket" },
          { tags: ["pocket-selection"] }
        );
      sel.apply(
        StateTransforms.Representation.StructureRepresentation3D,
        {
          type: {
            name: "molecular-surface",
            params: { alpha: 0.85, quality: "medium" },
          },
          colorTheme: {
            name: "uniform",
            params: { value: Color(hexToColor(pocket.color)) },
          },
        }
      );
      await builder.commit();
      // Track the selection ref so clear can remove the subtree.
      this.pocketRefs.push(sel.ref);
    } catch (e) {
      console.warn("showPocketSurface failed:", e);
    }
  }

  async clearPocketSurfaces(): Promise<void> {
    await this._removeRefs(this.pocketRefs);
    this.pocketRefs = [];
  }

  // Overpaint the existing cartoon with a uniform colour on the
  // cluster's residues — no new representation, just a colour overlay.
  async showClusterOverpaint(cluster: ClusterAnno): Promise<void> {
    const structure = this._firstStructure();
    if (!structure) return;
    const expression = residuesExpression(cluster.residues);
    if (!expression) return;

    try {
      // Find the cartoon representation on the polymer component.
      const polymerComp = structure.components.find(
        (c: any) => c.key === "structure-component-static-polymer"
      );
      if (!polymerComp || !polymerComp.representations.length) return;
      const cartoonRepr = polymerComp.representations[0];

      const update = this.plugin.state.data.build();
      const ref = update
        .to(cartoonRepr.cell.transform.ref)
        .apply(
          StateTransforms.Representation
            .OverpaintStructureRepresentation3DFromScript,
          {
            layers: [
              {
                script: {
                  language: "mol-script",
                  expression: this._exprToScript(cluster.residues),
                },
                color: Color(hexToColor(cluster.color)),
                clear: false,
              },
            ],
          },
          { tags: [`cluster-${cluster.cluster_id}-overpaint`] }
        )
        .ref;
      await update.commit();
      this.clusterRefs.push(ref);
    } catch (e) {
      console.warn("showClusterOverpaint failed:", e);
    }
  }

  async clearClusterOverpaints(): Promise<void> {
    await this._removeRefs(this.clusterRefs);
    this.clusterRefs = [];
  }

  // Load an SDF as a second structure and render it ball-and-stick.
  // Subsequent calls remove the previous pose first.
  async loadLigandPose(sdf: string, color: Hex): Promise<void> {
    try {
      if (this.ligandPoseRef) {
        await this._removeRefs([this.ligandPoseRef]);
        this.ligandPoseRef = null;
      }

      // Mol*'s Viewer wrapper exposes ``loadStructureFromData`` for
      // text data + format hint.
      await (this.viewer as any).loadStructureFromData(sdf, "sdf", {
        dataLabel: "ligand_pose",
      });

      // Track the most recently loaded structure as our pose ref.
      const structures = this.plugin.managers.structure.hierarchy.current.structures;
      if (structures.length > 0) {
        const last = structures[structures.length - 1];
        this.ligandPoseRef = last.cell.transform.ref;
        // Override the auto preset's colouring with a uniform colour.
        // (Mol*'s default auto preset gives ligands ball-and-stick
        // already; we just re-colour.)
        if (last.components.length > 0) {
          const comp = last.components[0];
          if (comp.representations.length > 0) {
            const reprRef = comp.representations[0].cell.transform.ref;
            const update = this.plugin.state.data.build();
            update.to(reprRef).update({
              colorTheme: {
                name: "uniform",
                params: { value: Color(hexToColor(color)) },
              },
            });
            await update.commit();
          }
        }
      }
    } catch (e) {
      console.warn("loadLigandPose failed:", e);
    }
  }

  async clearLigandPose(): Promise<void> {
    if (this.ligandPoseRef) {
      await this._removeRefs([this.ligandPoseRef]);
      this.ligandPoseRef = null;
    }
  }

  // Zoom the camera to the bounding sphere of the given residues.
  async focusOnResidues(residues: string[]): Promise<void> {
    const structure = this._firstStructure();
    if (!structure) return;
    const expression = residuesExpression(residues);
    if (!expression) return;
    try {
      const data = structure.cell.obj.data;
      const compiled = compile<any>(expression);
      const sel = compiled(new QueryContext(data));
      const loci = StructureSelection.toLociWithSourceUnits(sel);
      this.plugin.managers.camera.focusLoci(loci, {
        extraRadius: 4,
        durationMs: 400,
      });
    } catch (e) {
      console.warn("focusOnResidues failed:", e);
    }
  }

  async resetCamera(): Promise<void> {
    try {
      this.plugin.managers.camera.reset(undefined, true);
    } catch (e) {
      console.warn("resetCamera failed:", e);
    }
  }

  // Subscribe to residue-level click events. Returns an unsubscribe fn.
  onResidueClick(cb: (residueId: string | null) => void): () => void {
    if (this.clickUnsubscribe) {
      this.clickUnsubscribe();
      this.clickUnsubscribe = null;
    }
    try {
      const stream = this.plugin.canvas3d?.interaction?.click;
      if (!stream || typeof stream.subscribe !== "function") {
        console.warn("residue click stream unavailable");
        return () => {};
      }
      const sub = stream.subscribe(({ current }: any) => {
        try {
          if (!current?.loci || current.loci.kind === "empty-loci") {
            cb(null);
            return;
          }
          const loc = (current.loci as any).elements?.[0]?.unit
            ? this._lociFirstResidue(current.loci)
            : null;
          cb(loc);
        } catch (e) {
          console.warn("click decode failed:", e);
          cb(null);
        }
      });
      this.clickUnsubscribe = () => {
        try {
          sub.unsubscribe();
        } catch (_) {
          /* swallow */
        }
      };
      return this.clickUnsubscribe;
    } catch (e) {
      console.warn("onResidueClick failed:", e);
      return () => {};
    }
  }

  dispose(): void {
    if (this.clickUnsubscribe) this.clickUnsubscribe();
    this.viewer.dispose?.();
  }

  // ── internals ─────────────────────────────────────────────────────

  private _firstStructure(): any {
    const structures = this.plugin.managers.structure.hierarchy.current.structures;
    return structures && structures.length > 0 ? structures[0] : null;
  }

  private async _removeRefs(refs: string[]): Promise<void> {
    for (const ref of refs) {
      try {
        const update = this.plugin.state.data.build();
        update.delete(ref);
        await update.commit();
      } catch (e) {
        console.warn("delete ref failed:", ref, e);
      }
    }
  }

  // Render residue list as a MolScript text expression for the overpaint
  // transform (which accepts script-as-text, not Expression objects).
  private _exprToScript(residues: string[]): string {
    const clauses: string[] = [];
    for (const r of residues) {
      const idx = r.indexOf("_");
      if (idx < 0) continue;
      const chain = r.slice(0, idx);
      const num = r.slice(idx + 1);
      if (!chain || !num) continue;
      clauses.push(
        `(sel.atom.props auth_seq_id ${num} and sel.atom.props label_asym_id "${chain}")`
      );
    }
    if (clauses.length === 0) return "(sel.atom.all false)";
    return clauses.join(" or ");
  }

  private _lociFirstResidue(loci: any): string | null {
    try {
      const els = loci.elements;
      if (!els || els.length === 0) return null;
      const el = els[0];
      const unit = el.unit;
      const indices = el.indices;
      const idx =
        typeof indices.first === "function" ? indices.first() : indices[0];
      const eI = unit.elements[idx];
      const props = unit.model?.atomicHierarchy;
      if (!props) return null;
      const residueIdx = props.residueAtomSegments?.index?.[eI];
      const chain = props.chains?.label_asym_id?.value?.(
        props.residueAtomSegments?.chains?.[residueIdx] ?? residueIdx
      );
      const seqId = props.residues?.auth_seq_id?.value?.(residueIdx);
      if (chain == null || seqId == null) return null;
      return `${chain}_${seqId}`;
    } catch (_) {
      return null;
    }
  }
}

// Expose globally so the Streamlit-side inline JS can reach us.
(window as any).molstarBridge = { MolViewer };
