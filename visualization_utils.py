import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import nglview as nv
import tempfile
import os
import json
from pathlib import Path
import matplotlib.pyplot as plt
import seaborn as sns
import py3Dmol
import streamlit.components.v1 as components
import stmol  # For py3Dmol visualization in Streamlit
import time # Added for unique key generation

def create_3d_protein_viewer(pdb_file_path, pocket_residues=None, pocket_centers=None):
    """
    Create a 3D protein viewer with highlighted pockets using nglview
    
    Args:
        pdb_file_path: Path to the PDB file
        pocket_residues: List of residue IDs that form pockets
        pocket_centers: List of pocket center coordinates
    """
    try:
        # Create nglview widget
        view = nv.show_file(pdb_file_path)
        
        # Set initial view
        view.camera = 'orthographic'
        view.parameters = {
            'backgroundColor': 'white',
            'quality': 'medium'
        }
        
        # Add pocket highlighting if residues are provided
        if pocket_residues:
            # Create selection string for pocket residues
            residue_selection = " or ".join([f"resid {resid}" for resid in pocket_residues])
            view.add_representation('ball+stick', selection=residue_selection, color='red')
            
        # Add pocket centers as spheres if provided
        if pocket_centers:
            for i, center in enumerate(pocket_centers):
                view.add_shape('sphere', center, radius=3.0, color='orange')
        
        return view
        
    except Exception as e:
        st.error(f"Error creating 3D viewer: {str(e)}")
        return None

def render_structure_with_residues_stmol(pdb_data_str, residue_ids_str, key_suffix=""):
    """
    Render a 3D structure with residue highlighting using py3Dmol (preferred) or Plotly (fallback).
    
    Args:
        pdb_data_str: PDB data as string
        residue_ids_str: Space-separated string of residue IDs to highlight (e.g., "A_1019 A_1022")
        key_suffix: Unique suffix for Streamlit components
    """
    import streamlit as st
    import plotly.graph_objects as go
    import numpy as np
    
    # Try py3Dmol first (better visualization)
    try:
        import py3Dmol
        import stmol
        
        # Parse all unique residue numbers and chains from the PDB
        pdb_residues = set()
        for line in pdb_data_str.splitlines():
            if line.startswith('ATOM') or line.startswith('HETATM'):
                parts = line.split()
                if len(parts) >= 5:
                    chain = parts[4]
                    try:
                        resnum = int(parts[5])
                        pdb_residues.add((chain, resnum))
                    except ValueError:
                        continue
        
        # Show debug info
        pdb_residues_list = sorted(list(pdb_residues))[:10]  # Show first 10
        st.info(f"PDB residues (first 10): {pdb_residues_list}")
        st.info(f"Total PDB residues: {len(pdb_residues)}")
        
        if residue_ids_str and isinstance(residue_ids_str, str):
            try:
                # Parse residue IDs from CSV (e.g., "A_1019 A_1022" -> ["A_1019", "A_1022"])
                csv_residue_ids = residue_ids_str.strip().split()
                st.info(f"CSV residue IDs: {csv_residue_ids[:5]}... (showing first 5)")
                
                # Extract chain and residue numbers from CSV IDs
                csv_residues = []
                for residue_id in csv_residue_ids:
                    if '_' in residue_id:
                        chain, resnum_str = residue_id.split('_', 1)
                        try:
                            resnum = int(resnum_str)
                            csv_residues.append((chain, resnum))
                        except ValueError:
                            continue
                
                st.info(f"CSV residue numbers: {csv_residues[:5]}... (showing first 5)")
                
                # Find the offset between CSV and PDB residue numbers
                if csv_residues and pdb_residues:
                    # Calculate the average offset
                    csv_nums = [r[1] for r in csv_residues]
                    pdb_nums = [r[1] for r in pdb_residues]
                    
                    if csv_nums and pdb_nums:
                        avg_csv = sum(csv_nums) / len(csv_nums)
                        avg_pdb = sum(pdb_nums) / len(pdb_nums)
                        offset = avg_csv - avg_pdb
                        
                        st.info(f"Estimated offset: CSV avg={avg_csv:.1f}, PDB avg={avg_pdb:.1f}, offset={offset:.1f}")
                        
                        # Map CSV residues to PDB residues using the offset
                        mapped_residues = []
                        for chain, csv_resnum in csv_residues:
                            pdb_resnum = csv_resnum - int(offset)
                            if (chain, pdb_resnum) in pdb_residues:
                                mapped_residues.append((chain, pdb_resnum))
                        
                        st.info(f"Mapped residues (after offset correction): {mapped_residues[:5]}... (showing first 5)")
                        
                        if mapped_residues:
                            # Create selection string for py3Dmol
                            selection_parts = []
                            for chain, resnum in mapped_residues:
                                selection_parts.append(f"resid {resnum} and chain {chain}")
                            
                            if selection_parts:
                                selection_string = " or ".join(selection_parts)
                                st.info(f"Selection string: {selection_string[:100]}...")
                                
                                # Create py3Dmol viewer
                                viewer = py3Dmol.view(width=800, height=600)
                                viewer.addModel(pdb_data_str, "pdb")
                                viewer.setStyle({'cartoon': {'color': 'spectrum'}})
                                
                                # Add surface highlighting for mapped residues
                                viewer.addSurface(py3Dmol.VDW, {"opacity": 0.7, "color": "red"}, {"sele": selection_string})
                                
                                # Display with stmol
                                stmol.showmol(viewer, height=600)
                                st.success(f"✅ py3Dmol: Successfully highlighted {len(mapped_residues)} residues")
                                return  # Exit early if py3Dmol works
                            else:
                                st.warning("No valid selection string created after mapping")
                        else:
                            st.warning("No residues mapped after offset correction")
                    else:
                        st.warning("Could not calculate offset - no valid residue numbers")
                else:
                    st.warning("No valid residues found in CSV or PDB")
                    
            except Exception as e:
                st.error(f"Error processing residue IDs: {e}")
        
        # If we get here, py3Dmol failed, so fall back to Plotly
        st.warning("py3Dmol failed, falling back to Plotly visualization...")
        
    except ImportError:
        st.warning("py3Dmol not available, using Plotly visualization...")
    except Exception as e:
        st.warning(f"py3Dmol failed: {e}, falling back to Plotly visualization...")
    
    # Fallback to Plotly visualization
    # Parse PDB data to extract atom coordinates
    atoms = []
    highlighted_atoms = []
    
    # Parse all unique residue numbers and chains from the PDB
    pdb_residues = set()
    for line in pdb_data_str.splitlines():
        if line.startswith('ATOM') or line.startswith('HETATM'):
            parts = line.split()
            if len(parts) >= 9:
                chain = parts[4]
                try:
                    resnum = int(parts[5])
                    pdb_residues.add((chain, resnum))
                    
                    # Extract coordinates
                    x, y, z = float(parts[6]), float(parts[7]), float(parts[8])
                    atom_type = parts[2]
                    
                    atoms.append({
                        'x': x, 'y': y, 'z': z,
                        'atom_type': atom_type,
                        'chain': chain,
                        'resnum': resnum
                    })
                except ValueError:
                    continue
    
    # Show debug info
    pdb_residues_list = sorted(list(pdb_residues))[:10]  # Show first 10
    st.info(f"PDB residues (first 10): {pdb_residues_list}")
    st.info(f"Total PDB residues: {len(pdb_residues)}")
    st.info(f"Total atoms parsed: {len(atoms)}")
    
    if residue_ids_str and isinstance(residue_ids_str, str):
        try:
            # Parse residue IDs from CSV (e.g., "A_1019 A_1022" -> ["A_1019", "A_1022"])
            csv_residue_ids = residue_ids_str.strip().split()
            st.info(f"CSV residue IDs: {csv_residue_ids[:5]}... (showing first 5)")
            
            # Extract chain and residue numbers from CSV IDs
            csv_residues = []
            for residue_id in csv_residue_ids:
                if '_' in residue_id:
                    chain, resnum_str = residue_id.split('_', 1)
                    try:
                        resnum = int(resnum_str)
                        csv_residues.append((chain, resnum))
                    except ValueError:
                        continue
            
            st.info(f"CSV residue numbers: {csv_residues[:5]}... (showing first 5)")
            
            # Find the offset between CSV and PDB residue numbers
            if csv_residues and pdb_residues:
                # Calculate the average offset
                csv_nums = [r[1] for r in csv_residues]
                pdb_nums = [r[1] for r in pdb_residues]
                
                if csv_nums and pdb_nums:
                    avg_csv = sum(csv_nums) / len(csv_nums)
                    avg_pdb = sum(pdb_nums) / len(pdb_nums)
                    offset = avg_csv - avg_pdb
                    
                    st.info(f"Estimated offset: CSV avg={avg_csv:.1f}, PDB avg={avg_pdb:.1f}, offset={offset:.1f}")
                    
                    # Map CSV residues to PDB residues using the offset
                    mapped_residues = []
                    for chain, csv_resnum in csv_residues:
                        pdb_resnum = csv_resnum - int(offset)
                        if (chain, pdb_resnum) in pdb_residues:
                            mapped_residues.append((chain, pdb_resnum))
                    
                    st.info(f"Mapped residues (after offset correction): {mapped_residues[:5]}... (showing first 5)")
                    
                    # Mark atoms that belong to highlighted residues
                    for atom in atoms:
                        if (atom['chain'], atom['resnum']) in mapped_residues:
                            highlighted_atoms.append(atom)
                    
                    st.success(f"Successfully mapped {len(mapped_residues)} residues and found {len(highlighted_atoms)} highlighted atoms")
                else:
                    st.warning("Could not calculate offset - no valid residue numbers")
            else:
                st.warning("No valid residues found in CSV or PDB")
                
        except Exception as e:
            st.error(f"Error processing residue IDs: {e}")
    
    # Create 3D scatter plot with Plotly
    if atoms:
        # Separate atoms by type for better visualization
        ca_atoms = [a for a in atoms if a['atom_type'] == 'CA']  # Alpha carbons for backbone
        other_atoms = [a for a in atoms if a['atom_type'] != 'CA']
        
        fig = go.Figure()
        
        # Add all atoms as gray dots
        if other_atoms:
            fig.add_trace(go.Scatter3d(
                x=[a['x'] for a in other_atoms],
                y=[a['y'] for a in other_atoms],
                z=[a['z'] for a in other_atoms],
                mode='markers',
                marker=dict(size=1, color='lightgray', opacity=0.3),
                name='All atoms',
                showlegend=False
            ))
        
        # Add backbone (CA atoms) as blue line
        if ca_atoms:
            fig.add_trace(go.Scatter3d(
                x=[a['x'] for a in ca_atoms],
                y=[a['y'] for a in ca_atoms],
                z=[a['z'] for a in ca_atoms],
                mode='lines+markers',
                line=dict(color='blue', width=3),
                marker=dict(size=2, color='blue'),
                name='Protein backbone'
            ))
        
        # Add highlighted residues as red spheres
        if highlighted_atoms:
            fig.add_trace(go.Scatter3d(
                x=[a['x'] for a in highlighted_atoms],
                y=[a['y'] for a in highlighted_atoms],
                z=[a['z'] for a in highlighted_atoms],
                mode='markers',
                marker=dict(size=5, color='red', opacity=0.8),
                name='Predicted pocket residues'
            ))
        
        # Update layout
        fig.update_layout(
            title='3D Protein Structure with Predicted Pocket Residues (Plotly)',
            scene=dict(
                xaxis_title='X (Å)',
                yaxis_title='Y (Å)',
                zaxis_title='Z (Å)',
                aspectmode='data'
            ),
            width=800,
            height=600
        )
        
        # Display the plot
        st.plotly_chart(fig, use_container_width=True)
        st.success("✅ 3D protein structure displayed successfully with Plotly")
        
    else:
        st.error("❌ No atoms found in PDB data")
        # Fallback: show the PDB data as text
        st.text_area("PDB Data (fallback)", pdb_data_str[:1000] + "...", height=200)

def create_enhanced_3d_viewer(pdb_file_path, pocket_csv_path=None, frame_pocket_index=None):
    """
    Create an enhanced 3D viewer using py3Dmol and stmol.
    
    Args:
        pdb_file_path: Path to the PDB file
        pocket_csv_path: Path to pockets CSV file (optional)
        frame_pocket_index: Specific pocket to highlight (optional)
    
    Returns:
        HTML string for the viewer
    """
    try:
        # Read PDB file
        with open(pdb_file_path, 'r') as f:
            pdb_data = f.read()
        
        # Get surface atoms if CSV is provided
        surface_atoms_str = ""
        if pocket_csv_path and frame_pocket_index:
            try:
                # Try to find the individual prediction CSV file for this frame
                pdb_dir = os.path.dirname(pdb_file_path)
                pdb_filename = os.path.basename(pdb_file_path)
                frame_number = pdb_filename.split('_')[-1].replace('.pdb', '')
                
                # Look for the individual prediction CSV file
                prediction_csv_path = os.path.join(pdb_dir, f"trajectory_*_test_data_{frame_number}.pdb_predictions.csv")
                import glob
                matching_files = glob.glob(prediction_csv_path)
                
                if matching_files:
                    # Use the first matching file
                    individual_csv_path = matching_files[0]
                    df = pd.read_csv(individual_csv_path)
                    
                    # Debug: show available columns
                    st.info(f"Available columns in CSV: {list(df.columns)}")
                    
                    # Find the pocket with the matching index
                    # The CSV has column '  rank' (with leading spaces)
                    rank_column = '  rank'
                    if rank_column in df.columns:
                        pocket_data = df[df[rank_column] == frame_pocket_index]
                        if not pocket_data.empty:
                            # Try different possible column names for surface atom IDs
                            surface_atoms_str = ""
                            possible_column_names = [' surf_atom_ids', 'surf_atom_ids', ' surf_atoms', 'surf_atoms']
                            
                            for col_name in possible_column_names:
                                if col_name in df.columns:
                                    surface_atoms_str = str(pocket_data.iloc[0].get(col_name, ''))
                                    st.info(f"Found surface atoms using column '{col_name}': {len(surface_atoms_str.split()) if surface_atoms_str else 0} atoms")
                                    break
                            
                            if not surface_atoms_str:
                                st.warning(f"No surface atom column found. Available columns: {list(df.columns)}")
                        else:
                            st.warning(f"No pocket found with rank {frame_pocket_index}. Available ranks: {df[rank_column].tolist()}")
                    else:
                        st.warning(f"Rank column '{rank_column}' not found. Available columns: {list(df.columns)}")
                else:
                    st.warning(f"Could not find individual prediction CSV for frame {frame_number}")
                    
            except Exception as e:
                st.warning(f"Could not read pocket data: {e}")
        
        # Create viewer
        viewer = py3Dmol.view(width=800, height=600)
        viewer.addModel(pdb_data, "pdb")
        viewer.setStyle({'cartoon': {'color': 'spectrum'}})
        
        # Add surface highlighting if atoms are available
        if surface_atoms_str and isinstance(surface_atoms_str, str):
            try:
                st.info(f"Attempting to parse surface atoms string: '{surface_atoms_str[:100]}...'")
                atom_indices = [int(idx) for idx in surface_atoms_str.split()]
                if atom_indices:
                    viewer.addSurface(py3Dmol.VDW, {"opacity": 0.7, "color": "red"}, {"atom": atom_indices})
                    st.success(f"Successfully highlighted {len(atom_indices)} surface atoms")
                else:
                    st.warning("Surface atoms string was empty after parsing")
            except ValueError as e:
                st.warning(f"Surface atom indices could not be parsed. Error: {e}")
                st.info(f"Failed string: '{surface_atoms_str}'")
        else:
            st.info(f"Surface atoms string was empty or invalid: '{surface_atoms_str}'")
        
        viewer.zoomTo()
        return stmol.showmol(viewer, height=600, width=800)
        
    except Exception as e:
        st.error(f"Error creating 3D viewer: {e}")
        return None

def create_simple_3d_viewer(pdb_file_path, pocket_residues=None, pocket_centers=None):
    """
    Create a simple 3D viewer using HTML/JavaScript that works better with Streamlit
    
    Args:
        pdb_file_path: Path to the PDB file
        pocket_residues: List of residue IDs that form pockets
        pocket_centers: List of pocket center coordinates
    """
    try:
        # Read PDB file content
        with open(pdb_file_path, 'r') as f:
            pdb_content = f.read()

        # Build JS for pocket highlighting
        if pocket_residues:
            selection = " or ".join([f"resid {resid}" for resid in pocket_residues])
            highlight_js = f'''
                var selection = "{selection}";
                component.addRepresentation("ball+stick", {{
                    sele: selection,
                    color: "red"
                }});
            '''
        else:
            highlight_js = "// No pocket residues provided"

        # Then in your HTML:
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <script src="https://unpkg.com/ngl@0.10.4/dist/ngl.js"></script>
            <style>
                #viewport {{
                    width: 100%;
                    height: 600px;
                    border: 1px solid #ccc;
                }}
            </style>
        </head>
        <body>
            <div id="viewport"></div>
            <script>
                var stage = new NGL.Stage("viewport");
                stage.setParameters({{
                    backgroundColor: "white"
                }});
                var pdbData = `{pdb_content}`;
                stage.loadFile(new Blob([pdbData], {{type: "text/plain"}}), {{ext: "pdb"}})
                    .then(function (component) {{
                        component.addRepresentation("cartoon");
                        component.autoView();
                        {highlight_js}
                    }});
            </script>
        </body>
        </html>
        """
        return html_content

    except Exception as e:
        st.error(f"Error creating 3D viewer: {str(e)}")
        return None

def create_pocket_heatmap(predictions_dir):
    """
    Create a heatmap showing pocket presence across frames
    
    Args:
        predictions_dir: Directory containing prediction CSV files
    """
    try:
        # Collect all prediction files
        prediction_files = sorted([f for f in os.listdir(predictions_dir) 
                                 if f.endswith('_predictions.csv')])
        
        if not prediction_files:
            st.warning("No prediction CSV files found")
            return None
        
        heatmap_data = []
        pocket_names = set()
        
        for file in prediction_files:
            try:
                # Try to extract frame number from filename
                # Expected format: something_frame_number_predictions.csv
                parts = file.split('_')
                if len(parts) < 3:
                    st.warning(f"Skipping file with unexpected format: {file}")
                    continue
                
                # Look for numeric parts that could be frame numbers
                frame_num = None
                for part in parts:
                    if part.isdigit():
                        frame_num = int(part)
                        break
                
                if frame_num is None:
                    st.warning(f"Could not extract frame number from filename: {file}")
                    continue
                
                file_path = os.path.join(predictions_dir, file)
                df = pd.read_csv(file_path)
                
                # Check for required columns (handle leading/trailing spaces in column names)
                required_columns = ['name ', ' score', ' probability']
                if not all(col in df.columns for col in required_columns):
                    st.warning(f"Skipping file with missing columns: {file}")
                    st.info(f"Available columns: {list(df.columns)}")
                    continue
                
                # Filter out rows with invalid data
                df = df.dropna(subset=[' score', ' probability'])
                df = df[df[' score'].notna() & df[' probability'].notna()]
                
                if df.empty:
                    st.warning(f"No valid data in file: {file}")
                    continue
                
                for _, row in df.iterrows():
                    pocket_name = row['name ']
                    pocket_names.add(pocket_name)
                    heatmap_data.append({
                        'frame': frame_num,
                        'pocket': pocket_name,
                        'score': row[' score'],
                        'probability': row[' probability']
                    })
                    
            except (ValueError, IndexError) as e:
                st.warning(f"Error processing file {file}: {str(e)}")
                continue
            except Exception as e:
                st.warning(f"Unexpected error processing file {file}: {str(e)}")
                continue
        
        if not heatmap_data:
            st.warning("No valid data found in any prediction files")
            return None
            
        heatmap_df = pd.DataFrame(heatmap_data)
        pocket_names = sorted(list(pocket_names))
        
        # Create pivot table for heatmap
        pivot_df = heatmap_df.pivot(index='pocket', columns='frame', values='score').fillna(0)
        
        # Create heatmap
        fig = px.imshow(
            pivot_df,
            title="Pocket Presence Heatmap Across Frames",
            labels={'x': 'Frame Number', 'y': 'Pocket Name', 'color': 'Score'},
            color_continuous_scale='Reds'
        )
        
        fig.update_layout(height=500)
        return fig
        
    except Exception as e:
        st.error(f"Error creating heatmap: {str(e)}")
        return None

def create_pocket_summary_chart(predictions_csv_path):
    """
    Create a summary chart of pocket predictions
    
    Args:
        predictions_csv_path: Path to the predictions CSV file
    """
    try:
        df = pd.read_csv(predictions_csv_path)
        
        # Check for required columns
        required_columns = ['score', 'size', 'name']
        missing_columns = [col for col in required_columns if col not in df.columns]
        
        if missing_columns:
            st.warning(f"Missing required columns in CSV: {missing_columns}")
            return None
        
        # Filter out rows with invalid data
        df = df.dropna(subset=['score', 'size'])
        df = df[df['score'].notna() & df['size'].notna()]
        
        if df.empty:
            st.warning("No valid data found in CSV file")
            return None
        
        # Create summary statistics
        fig = make_subplots(
            rows=2, cols=2,
            subplot_titles=('Pocket Scores Distribution', 'Pocket Sizes', 'Score vs Size', 'Top Pockets'),
            specs=[[{"type": "histogram"}, {"type": "histogram"}],
                   [{"type": "scatter"}, {"type": "bar"}]]
        )
        
        # Pocket scores distribution
        fig.add_trace(
            go.Histogram(x=df['score'], name='Score Distribution', nbinsx=20),
            row=1, col=1
        )
        
        # Pocket sizes distribution
        fig.add_trace(
            go.Histogram(x=df['size'], name='Size Distribution', nbinsx=20),
            row=1, col=2
        )
        
        # Score vs Size scatter
        fig.add_trace(
            go.Scatter(x=df['size'], y=df['score'], mode='markers', 
                      text=df['name'], name='Score vs Size'),
            row=2, col=1
        )
        
        # Top pockets bar chart
        top_pockets = df.nlargest(10, 'score')
        fig.add_trace(
            go.Bar(x=top_pockets['name'], y=top_pockets['score'], name='Top Pockets'),
            row=2, col=2
        )
        
        fig.update_layout(height=600, title_text="Pocket Analysis Summary")
        return fig
        
    except Exception as e:
        st.error(f"Error creating summary chart: {str(e)}")
        return None

def create_cluster_visualization(clustered_csv_path):
    """
    Create cluster visualization
    
    Args:
        clustered_csv_path: Path to the clustered pockets CSV file
    """
    try:
        df = pd.read_csv(clustered_csv_path)
        
        # Check for required columns
        required_columns = ['score', 'size', 'cluster']
        missing_columns = [col for col in required_columns if col not in df.columns]
        
        if missing_columns:
            st.warning(f"Missing required columns in CSV: {missing_columns}")
            return None, None
        
        # Filter out rows with invalid data
        df = df.dropna(subset=['score', 'size', 'cluster'])
        df = df[df['score'].notna() & df['size'].notna() & df['cluster'].notna()]
        
        if df.empty:
            st.warning("No valid data found in CSV file")
            return None, None
        
        # Create cluster summary
        cluster_summary = df.groupby('cluster').agg({
            'score': ['mean', 'std', 'count'],
            'size': ['mean', 'std']
        }).round(3)
        
        # Create cluster visualization
        fig = make_subplots(
            rows=2, cols=2,
            subplot_titles=('Clusters by Score', 'Clusters by Size', 'Cluster Distribution', 'Score vs Size by Cluster'),
            specs=[[{"type": "box"}, {"type": "box"}],
                   [{"type": "histogram"}, {"type": "scatter"}]]
        )
        
        # Box plots for each cluster
        for cluster_id in df['cluster'].unique():
            cluster_data = df[df['cluster'] == cluster_id]
            
            fig.add_trace(
                go.Box(y=cluster_data['score'], name=f'Cluster {cluster_id}'),
                row=1, col=1
            )
            
            fig.add_trace(
                go.Box(y=cluster_data['size'], name=f'Cluster {cluster_id}'),
                row=1, col=2
            )
        
        # Cluster distribution
        cluster_counts = df['cluster'].value_counts()
        fig.add_trace(
            go.Bar(x=cluster_counts.index, y=cluster_counts.values, name='Cluster Sizes'),
            row=2, col=1
        )
        
        # Score vs Size colored by cluster
        for cluster_id in df['cluster'].unique():
            cluster_data = df[df['cluster'] == cluster_id]
            fig.add_trace(
                go.Scatter(x=cluster_data['size'], y=cluster_data['score'], 
                          mode='markers', name=f'Cluster {cluster_id}'),
                row=2, col=2
            )
        
        fig.update_layout(height=600, title_text="Pocket Clustering Analysis")
        return fig, cluster_summary
        
    except Exception as e:
        st.error(f"Error creating cluster visualization: {str(e)}")
        return None, None

def create_timeline_visualization(predictions_dir):
    """
    Create a timeline visualization showing pocket evolution across frames
    
    Args:
        predictions_dir: Directory containing prediction CSV files
    """
    try:
        # Collect all prediction files
        prediction_files = sorted([f for f in os.listdir(predictions_dir) 
                                 if f.endswith('_predictions.csv')])
        
        if not prediction_files:
            st.warning("No prediction CSV files found")
            return None
        
        timeline_data = []
        for file in prediction_files:
            try:
                # Try to extract frame number from filename
                # Expected format: something_frame_number_predictions.csv
                parts = file.split('_')
                if len(parts) < 3:
                    st.warning(f"Skipping file with unexpected format: {file}")
                    continue
                
                # Look for numeric parts that could be frame numbers
                frame_num = None
                for part in parts:
                    if part.isdigit():
                        frame_num = int(part)
                        break
                
                if frame_num is None:
                    st.warning(f"Could not extract frame number from filename: {file}")
                    continue
                
                file_path = os.path.join(predictions_dir, file)
                df = pd.read_csv(file_path)
                
                # Check for required columns (handle leading/trailing spaces in column names)
                required_columns = ['name ', ' score', ' probability']
                if not all(col in df.columns for col in required_columns):
                    st.warning(f"Skipping file with missing columns: {file}")
                    st.info(f"Available columns: {list(df.columns)}")
                    continue
                
                # Filter out rows with invalid data
                df = df.dropna(subset=[' score', ' probability'])
                df = df[df[' score'].notna() & df[' probability'].notna()]
                
                if df.empty:
                    st.warning(f"No valid data in file: {file}")
                    continue
                
                for _, row in df.iterrows():
                    timeline_data.append({
                        'frame': frame_num,
                        'pocket_name': row['name '],
                        'score': row[' score'],
                        'probability': row[' probability']
                    })
                    
            except (ValueError, IndexError) as e:
                st.warning(f"Error processing file {file}: {str(e)}")
                continue
            except Exception as e:
                st.warning(f"Unexpected error processing file {file}: {str(e)}")
                continue
        
        if not timeline_data:
            st.warning("No valid data found in any prediction files")
            return None
        
        timeline_df = pd.DataFrame(timeline_data)
        
        # Create timeline visualization
        fig = go.Figure()
        
        # Add pocket evolution lines
        for pocket_name in timeline_df['pocket_name'].unique():
            pocket_data = timeline_df[timeline_df['pocket_name'] == pocket_name]
            fig.add_trace(go.Scatter(
                x=pocket_data['frame'],
                y=pocket_data['score'],
                mode='lines+markers',
                name=pocket_name,
                hovertemplate=f'Frame: %{{x}}<br>Score: %{{y:.3f}}<br>Pocket: {pocket_name}<extra></extra>'
            ))
        
        fig.update_layout(
            title="Pocket Evolution Across Trajectory",
            xaxis_title="Frame Number",
            yaxis_title="Pocket Score",
            height=500
        )
        
        return fig
        
    except Exception as e:
        st.error(f"Error creating timeline visualization: {str(e)}")
        return None

def create_interactive_results_display(results_dir, job_id):
    """
    Create an interactive results display with 3D visualization and charts
    
    Args:
        results_dir: Path to the results directory
        job_id: Job ID for the analysis
    """
    
    st.markdown("### 🎯 Interactive Results Visualization")
    
    # Create tabs for different visualizations
    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
        "📊 Summary Statistics", 
        "🎨 3D Protein Viewer", 
        "📈 Pocket Analysis", 
        "🔗 Clustering Results", 
        "⏱️ Timeline Evolution",
        "🔥 Pocket Heatmap"
    ])
    
    with tab1:
        st.markdown("#### Summary Statistics")
        
        # Display basic metrics
        col1, col2, col3, col4 = st.columns(4)
        
        # Count files in different directories
        pdbs_dir = os.path.join(results_dir, "pdbs")
        pockets_dir = os.path.join(results_dir, "pockets")
        clusters_dir = os.path.join(results_dir, "pocket_clusters")
        
        with col1:
            if os.path.exists(pdbs_dir):
                pdb_count = len([f for f in os.listdir(pdbs_dir) if f.endswith('.pdb')])
                st.metric("Frames Extracted", pdb_count)
        
        with col2:
            if os.path.exists(pockets_dir):
                pockets_csv = os.path.join(pockets_dir, "pockets.csv")
                if os.path.exists(pockets_csv):
                    try:
                        pockets_df = pd.read_csv(pockets_csv)
                        st.metric("Total Pockets", len(pockets_df))
                    except Exception as e:
                        st.metric("Total Pockets", "Error")
                        st.error(f"Error reading pockets CSV: {str(e)}")
        
        with col3:
            if os.path.exists(clusters_dir):
                clustered_csv = os.path.join(clusters_dir, "pockets_clustered.csv")
                if os.path.exists(clustered_csv):
                    try:
                        clustered_df = pd.read_csv(clustered_csv)
                        unique_clusters = clustered_df['cluster'].nunique()
                        st.metric("Clusters Found", unique_clusters)
                    except Exception as e:
                        st.metric("Clusters Found", "Error")
                        st.error(f"Error reading clustered CSV: {str(e)}")
        
        with col4:
            if os.path.exists(clusters_dir):
                reps_csv = os.path.join(clusters_dir, "cluster_representatives.csv")
                if os.path.exists(reps_csv):
                    try:
                        reps_df = pd.read_csv(reps_csv)
                        st.metric("Representatives", len(reps_df))
                    except Exception as e:
                        st.metric("Representatives", "Error")
                        st.error(f"Error reading representatives CSV: {str(e)}")
    
    with tab2:
        st.markdown("#### 3D Protein Structure Viewer")
        
        # Select a PDB file to visualize
        if os.path.exists(pdbs_dir):
            pdb_files = [f for f in os.listdir(pdbs_dir) if f.endswith('.pdb')]
            if pdb_files:
                selected_pdb = st.selectbox(
                    "Select frame to visualize:",
                    pdb_files,
                    index=0
                )
                
                pdb_path = os.path.join(pdbs_dir, selected_pdb)
                
                # Get pocket information for this frame
                frame_name = selected_pdb.split('_')[-1].replace('.pdb', '')
                predictions_file = os.path.join(pockets_dir, "p2rank_output", f"{frame_name}_predictions.csv")
                
                pocket_residues = []
                pocket_centers = []
                surface_atom_indices = None
                
                if os.path.exists(predictions_file):
                    try:
                        pred_df = pd.read_csv(predictions_file)
                        
                        # Get top pocket for visualization
                        if not pred_df.empty:
                            top_pocket = pred_df.iloc[0]
                            try:
                                # Parse residue IDs from the actual column name
                                residue_ids_str = str(top_pocket.get('residue_ids', ''))
                                if residue_ids_str and residue_ids_str != 'nan':
                                    # Extract residue numbers from format like "A_1019 A_1022 A_1023"
                                    residue_parts = residue_ids_str.split()
                                    pocket_residues = []
                                    for part in residue_parts:
                                        if '_' in part:
                                            # Extract the number after underscore (e.g., "A_1019" -> 1019)
                                            residue_num = part.split('_')[1]
                                            if residue_num.isdigit():
                                                pocket_residues.append(int(residue_num))
                                
                                # Parse center coordinates from separate x, y, z columns
                                center_x = top_pocket.get('center_x')
                                center_y = top_pocket.get('center_y')
                                center_z = top_pocket.get('center_z')
                                if center_x is not None and center_y is not None and center_z is not None:
                                    try:
                                        pocket_centers = [[float(center_x), float(center_y), float(center_z)]]
                                    except (ValueError, TypeError):
                                        pocket_centers = []
                                
                                # Parse surface atom indices
                                surface_atom_indices = str(top_pocket.get('surf_atom_ids', ''))
                                
                                # Debug information
                                st.info(f"Found pocket data: {len(pocket_residues)} residues, center: {pocket_centers}, surface atoms: {len(surface_atom_indices.split()) if surface_atom_indices else 0}")
                                
                                # Show the actual residue IDs from CSV for debugging
                                st.info(f"CSV residue IDs: {residue_ids_str[:200]}...")
                                
                                # Try to find matching residues in PDB by reading the file
                                try:
                                    with open(pdb_path, 'r') as f:
                                        pdb_content = f.read()
                                    
                                    # Extract actual residue numbers from PDB
                                    pdb_residues = set()
                                    for line in pdb_content.split('\n'):
                                        if line.startswith('ATOM') or line.startswith('HETATM'):
                                            parts = line.split()
                                            if len(parts) >= 5:
                                                try:
                                                    pdb_residues.add(int(parts[5]))  # Residue number
                                                except (ValueError, IndexError):
                                                    continue
                                    
                                    st.info(f"PDB residue numbers range: {min(pdb_residues)} to {max(pdb_residues)}")
                                    st.info(f"CSV residue numbers: {pocket_residues[:10]}...")
                                    
                                    # Check if any CSV residues match PDB residues
                                    matching_residues = [r for r in pocket_residues if r in pdb_residues]
                                    st.info(f"Matching residues: {len(matching_residues)} out of {len(pocket_residues)}")
                                    
                                    # Use matching residues if any found, otherwise use original
                                    if matching_residues:
                                        pocket_residues = matching_residues
                                        st.success(f"Using {len(matching_residues)} matching residues for highlighting")
                                    else:
                                        st.warning("No matching residues found between CSV and PDB. Using original residue numbers.")
                                        
                                except Exception as e:
                                    st.warning(f"Could not analyze PDB residue numbers: {str(e)}")
                                
                            except (ValueError, TypeError) as e:
                                st.warning(f"Could not parse pocket data: {str(e)}")
                    except Exception as e:
                        st.warning(f"Error reading predictions file: {str(e)}")
                
                # Try enhanced 3D viewer first, fallback to simple viewer
                st.markdown("**Enhanced 3D Viewer (py3Dmol)**")
                
                # Try to find the individual prediction CSV file for this frame
                individual_csv_path = None
                try:
                    # Look for the individual prediction CSV file
                    prediction_csv_pattern = os.path.join(pockets_dir, "p2rank_output", f"trajectory_*_test_data_{frame_name}.pdb_predictions.csv")
                    import glob
                    matching_files = glob.glob(prediction_csv_pattern)
                    
                    if matching_files:
                        individual_csv_path = matching_files[0]
                        st.info(f"Found individual prediction CSV: {os.path.basename(individual_csv_path)}")
                        
                        # Read the individual CSV to get pocket data
                        df_individual = pd.read_csv(individual_csv_path)
                        st.info(f"Available columns in individual CSV: {list(df_individual.columns)}")
                        
                        # Find the pocket with the matching index from merged CSV
                        if 'pocket_index' in df.columns:
                            pocket_idx = df.iloc[0]['pocket_index']  # Get the pocket index from merged CSV
                            st.info(f"Looking for pocket index: {pocket_idx}")
                            
                            # Try to find matching pocket in individual CSV
                            rank_column = '  rank'  # Individual CSV has '  rank' column
                            if rank_column in df_individual.columns:
                                matching_pocket = df_individual[df_individual[rank_column] == pocket_idx]
                                if not matching_pocket.empty:
                                    st.success(f"Found matching pocket in individual CSV")
                                    enhanced_success = create_enhanced_3d_viewer(
                                        pdb_path, 
                                        individual_csv_path, 
                                        pocket_idx  # Use the actual pocket index
                                    )
                                else:
                                    st.warning(f"No pocket with rank {pocket_idx} found in individual CSV. Available ranks: {df_individual[rank_column].tolist()}")
                                    enhanced_success = None
                            else:
                                st.warning(f"Rank column '{rank_column}' not found in individual CSV")
                                enhanced_success = None
                        else:
                            st.warning("No pocket_index column in merged CSV")
                            enhanced_success = None
                    else:
                        st.warning(f"Could not find individual prediction CSV for frame {frame_name}")
                        enhanced_success = None
                except Exception as e:
                    st.warning(f"Error finding prediction CSV: {str(e)}")
                    enhanced_success = None
                
                if not enhanced_success:
                    st.markdown("**Fallback 3D Viewer (NGL.js)**")
                    html_content = create_simple_3d_viewer(pdb_path, pocket_residues, pocket_centers)
                    if html_content:
                        st.components.v1.html(html_content, height=600)
                    else:
                        st.error("Could not create 3D viewer")
            else:
                st.warning("No PDB files found for visualization")
    
    with tab3:
        st.markdown("#### Pocket Analysis")
        
        # Show pocket summary chart
        pockets_csv = os.path.join(pockets_dir, "pockets.csv")
        if os.path.exists(pockets_csv):
            fig = create_pocket_summary_chart(pockets_csv)
            if fig:
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.warning("No pocket analysis data found")
    
    with tab4:
        st.markdown("#### Clustering Results")
        
        clustered_csv = os.path.join(clusters_dir, "pockets_clustered.csv")
        if os.path.exists(clustered_csv):
            fig, cluster_summary = create_cluster_visualization(clustered_csv)
            if fig:
                st.plotly_chart(fig, use_container_width=True)
            
            if cluster_summary is not None:
                st.markdown("#### Cluster Summary Statistics")
                st.dataframe(cluster_summary, use_container_width=True)
        else:
            st.warning("No clustering results found")
    
    with tab5:
        st.markdown("#### Pocket Evolution Timeline")
        
        predictions_dir = os.path.join(pockets_dir, "p2rank_output")
        if os.path.exists(predictions_dir):
            fig = create_timeline_visualization(predictions_dir)
            if fig:
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.warning("No timeline data found")
    
    with tab6:
        st.markdown("#### Pocket Presence Heatmap")
        
        predictions_dir = os.path.join(pockets_dir, "p2rank_output")
        if os.path.exists(predictions_dir):
            fig = create_pocket_heatmap(predictions_dir)
            if fig:
                st.plotly_chart(fig, use_container_width=True)
        else:
            st.warning("No heatmap data found")

def create_download_section(results_dir, job_id=None):
    """
    Create a download section for all generated files
    
    Args:
        results_dir: Path to the results directory
        job_id: Optional job ID to make keys unique
    """
    st.markdown("### 📁 Download Results")
    
    # Create a unique suffix for keys
    key_suffix = f"_{job_id}" if job_id else f"_{int(time.time())}"
    
    # Create a ZIP file of all results
    import zipfile
    import io
    
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for root, dirs, files in os.walk(results_dir):
            for file in files:
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, results_dir)
                zip_file.write(file_path, arcname)
    
    zip_buffer.seek(0)
    
    # Download button for complete results
    st.download_button(
        label="📦 Download All Results (ZIP)",
        data=zip_buffer.getvalue(),
        file_name=f"pockethunter_results.zip",
        mime="application/zip",
        key=f"download_all_results{key_suffix}"
    )
    
    # Individual file downloads
    st.markdown("#### Individual Files")
    
    col1, col2 = st.columns(2)
    
    with col1:
        # PDB files
        pdbs_dir = os.path.join(results_dir, "pdbs")
        if os.path.exists(pdbs_dir):
            st.markdown("**📄 PDB Files**")
            pdb_files = [f for f in os.listdir(pdbs_dir) if f.endswith('.pdb')]
            for i, pdb_file in enumerate(pdb_files[:5]):  # Show first 5
                pdb_path = os.path.join(pdbs_dir, pdb_file)
                with open(pdb_path, 'rb') as f:
                    st.download_button(
                        label=f"Download {pdb_file}",
                        data=f.read(),
                        file_name=pdb_file,
                        mime="chemical/x-pdb",
                        key=f"download_pdb_{i}_{pdb_file}{key_suffix}"
                    )
    
    with col2:
        # CSV files
        st.markdown("**📊 CSV Files**")
        
        # Pockets CSV
        pockets_csv = os.path.join(results_dir, "pockets", "pockets.csv")
        if os.path.exists(pockets_csv):
            with open(pockets_csv, 'rb') as f:
                st.download_button(
                    label="Download pockets.csv",
                    data=f.read(),
                    file_name="pockets.csv",
                    mime="text/csv",
                    key=f"download_pockets_csv{key_suffix}"
                )
        
        # Clustered CSV
        clustered_csv = os.path.join(results_dir, "pocket_clusters", "pockets_clustered.csv")
        if os.path.exists(clustered_csv):
            with open(clustered_csv, 'rb') as f:
                st.download_button(
                    label="Download clustered_pockets.csv",
                    data=f.read(),
                    file_name="clustered_pockets.csv",
                    mime="text/csv",
                    key=f"download_clustered_csv{key_suffix}"
                ) 
def show_molecule_3d_with_pocket(pdb_path: str, pocket_residues: list,
                                  width: int = 400, height: int = 380,
                                  border_radius: int = 10,
                                  extra_height: int = 40) -> None:
    """Render a py3Dmol viewer with pocket residues highlighted in orange."""
    try:
        with open(pdb_path, 'r') as f:
            pdb_data = f.read()
        specs = []
        for res_str in pocket_residues:
            parts = res_str.strip().split('_', 1)
            if len(parts) == 2:
                try:
                    specs.append({'chain': parts[0], 'resi': int(parts[1])})
                except ValueError:
                    pass
        view = py3Dmol.view(width=width, height=height)
        view.addModel(pdb_data, 'pdb')
        view.setStyle({}, {'cartoon': {'color': 'spectrum'}})
        for s in specs:
            view.setStyle({'chain': s['chain'], 'resi': s['resi']},
                          {'stick': {'color': 'orange', 'radius': 0.3}})
        if specs:
            chains = {}
            for s in specs:
                chains.setdefault(s['chain'], []).append(s['resi'])
            for chain, resis in chains.items():
                view.addSurface(py3Dmol.VDW, {'opacity': 0.4, 'color': 'orange'},
                                {'chain': chain, 'resi': resis})
            view.zoomTo({'resi': [s['resi'] for s in specs]})
        else:
            view.zoomTo()
        view.spin(False)
        html = f'<div style="border-radius:{border_radius}px;overflow:hidden;">{view._make_html()}</div>'
        components.html(html, height=height + extra_height, scrolling=False)
    except Exception as e:
        st.error(f"Error loading 3D structure: {e}")
