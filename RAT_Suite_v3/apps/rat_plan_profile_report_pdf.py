# apps/rat_plan_profile_report_pdf.py

# This software was developed by the Federal Highway Administration (FHWA), 
# an agency of the U.S. Department of Transportation (U.S. DOT).
#
# To the extent possible under law, the Federal Highway Administration (FHWA) 
# has waived all copyright and related or neighboring rights to this software.
#
# This software is dedicated to the public domain under the CC0 1.0 Universal 
# Public Domain Dedication. You can copy, modify, distribute, and perform the 
# work, even for commercial purposes, all without asking permission.
#
# For more information, please see the LICENSE file or visit:
# https://creativecommons.org/publicdomain/zero/1.0/

"""
RAT PLAN & PROFILE PDF RENDERER (Matplotlib Generator)
--------------------------------------------------------------------------------
ROLE: Standalone visual engineering plan and profile sheet generator.
DESCRIPTION: 
Reads pre-processed vertex and curve CSVs to generate professional, multi-page 
engineering PDFs. It fetches USGS aerial basemaps, rotates geometries so the 
alignment flows left-to-right, and uses Matplotlib GridSpec to stack the Plan 
(top-down) and Profile (elevation) views with full curve annotations.
CREATED BY: FHWA, Office of Highway Policy Information using Google Gemini and
ChatGPT.
CREATED ON: 4/23/2026
"""
import os
import argparse
import math
import datetime
from io import BytesIO
import requests
import numpy as np
import pandas as pd
from PIL import Image
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Rectangle
from pyproj import Transformer
# --- Constants ---
FEET_PER_METER = 3.28084
PAGE_LENGTH_FT = 1500.0
BASEMAP_URL = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/export"
def get_utm_zone(lon, lat):
    zone = int((lon + 180) / 6) + 1
    return (32600 if lat >= 0 else 32700) + zone
def rotate_points(x_coords, y_coords, origin_x, origin_y, angle_rad):
    px = x_coords - origin_x
    py = y_coords - origin_y
    qx = px * np.cos(angle_rad) - py * np.sin(angle_rad)
    qy = px * np.sin(angle_rad) + py * np.cos(angle_rad)
    return qx, qy
def fetch_basemap_image(min_lon, min_lat, max_lon, max_lat, width=1200, height=600):
    bbox = f"{min_lon},{min_lat},{max_lon},{max_lat}"
    params = {
        "bbox": bbox, "bboxSR": "4326", "size": f"{width},{height}", 
        "imageSR": "4326", "format": "jpg", "f": "image"
    }
    try:
        r = requests.get(BASEMAP_URL, params=params, timeout=5)
        if r.status_code == 200: 
            return Image.open(BytesIO(r.content))
    except: 
        pass
    return None
def make_pdf(vertices_csv, horizontal_csv, vertical_csv, pdf_out, route_id):
    vtx = pd.read_csv(vertices_csv)
    hc = pd.read_csv(horizontal_csv) if os.path.exists(horizontal_csv) else pd.DataFrame()
    vc = pd.read_csv(vertical_csv) if os.path.exists(vertical_csv) else pd.DataFrame()
    required = {"Dist_Ft", "Elev_Ft", "Milepost", "Lon", "Lat", "Raw_Lon", "Raw_Lat", "Elev_Raw_Ft"}
    missing = required - set(vtx.columns)
    if missing:
        raise ValueError(f"Vertices CSV missing required columns: {sorted(missing)}")
    lon_start, lat_start = vtx['Lon'].iloc[0], vtx['Lat'].iloc[0]
    utm = get_utm_zone(lon_start, lat_start)
    trans = Transformer.from_crs("EPSG:4326", f"EPSG:{utm}", always_xy=True)
    utm_coords = np.array([trans.transform(lon, lat) for lon, lat in zip(vtx['Lon'], vtx['Lat'])])
    x_ft = utm_coords[:, 0] * FEET_PER_METER
    y_ft = utm_coords[:, 1] * FEET_PER_METER
    raw_utm_coords = np.array([trans.transform(lon, lat) for lon, lat in zip(vtx['Raw_Lon'], vtx['Raw_Lat'])])
    raw_x_ft = raw_utm_coords[:, 0] * FEET_PER_METER
    raw_y_ft = raw_utm_coords[:, 1] * FEET_PER_METER
    raw_elev_ft = vtx["Elev_Raw_Ft"].to_numpy()
    
    dist_ft = vtx["Dist_Ft"].to_numpy()
    elev_ft = vtx["Elev_Ft"].to_numpy()
    mp_array = vtx["Milepost"].to_numpy()
    total_dist = dist_ft[-1] - dist_ft[0]
    num_pages = max(1, int(math.ceil(total_dist / PAGE_LENGTH_FT)))
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    os.makedirs(os.path.dirname(pdf_out) or ".", exist_ok=True)
    with PdfPages(pdf_out) as pdf:
        for p in range(num_pages):
            page_start_dist = p * PAGE_LENGTH_FT
            page_end_dist = (p + 1) * PAGE_LENGTH_FT
            
            mask = (dist_ft >= page_start_dist) & (dist_ft < page_end_dist)
            if not mask.any(): continue
            
            sub_x = x_ft[mask]
            sub_y = y_ft[mask]
            sub_raw_x = raw_x_ft[mask]
            sub_raw_y = raw_y_ft[mask]
            sub_raw_elev = raw_elev_ft[mask]
            sub_mp = mp_array[mask]
            sub_dist = dist_ft[mask]
            sub_elev = elev_ft[mask]
            sub_lon = vtx['Lon'].to_numpy()[mask]
            sub_lat = vtx['Lat'].to_numpy()[mask]
            if len(sub_x) < 2: continue
            p_start_x, p_start_y = sub_x[0], sub_y[0]
            p_end_x, p_end_y = sub_x[-1], sub_y[-1]
            angle = math.atan2(p_end_y - p_start_y, p_end_x - p_start_x)
            
            rot_x, rot_y = rotate_points(sub_x, sub_y, p_start_x, p_start_y, -angle)
            raw_rot_x, raw_rot_y = rotate_points(sub_raw_x, sub_raw_y, p_start_x, p_start_y, -angle)

            mp_min, mp_max = sub_mp.min(), sub_mp.max()
            x_min, x_max = rot_x.min(), rot_x.max()
            if x_max > x_min:
                scale = (mp_max - mp_min) / (x_max - x_min)
                rot_x = mp_min + (rot_x - x_min) * scale
                raw_rot_x = mp_min + (raw_rot_x - x_min) * scale
                
            margin = 0.002
            
            margin = 0.002
            bg_img = fetch_basemap_image(min(sub_lon)-margin, min(sub_lat)-margin, max(sub_lon)+margin, max(sub_lat)+margin)
            fig = plt.figure(figsize=(17, 11))
            rect = Rectangle((0.02, 0.02), 0.96, 0.96, linewidth=2, edgecolor='black', facecolor='none', transform=fig.transFigure)
            fig.add_artist(rect)
            fig.text(0.05, 0.94, f"PROJECT: {route_id}", fontsize=14, fontweight='bold')
            fig.text(0.85, 0.94, f"SHEET {p+1} OF {num_pages}", fontsize=14, fontweight='bold')
            fig.text(0.85, 0.92, f"DATE: {date_str}", fontsize=10)
            
            gs = GridSpec(2, 1, height_ratios=[1, 1], hspace=0.3, left=0.08, right=0.92, top=0.88, bottom=0.08)
            
            # --- TOP: PLAN VIEW ---
            ax_plan = fig.add_subplot(gs[0])
            
            # Line characteristics
            ax_plan.plot(rot_x, rot_y, 'k-', linewidth=2.5, label="Centerline", zorder=2)
            ax_plan.plot(raw_rot_x, raw_rot_y, color='red', linestyle='--', linewidth=1.0, label="Raw Input Geom", zorder=1)
            ax_plan.axhline(0, color='gray', linestyle=':', linewidth=0.5)
            last_label_x = -9999
            colors_h = {'Left': 'blue', 'Right': 'red'}
            if not hc.empty:
                for _, c in hc.iterrows():
                    c_start_ft = c['Start_Dist'] * FEET_PER_METER
                    c_end_ft = c['End_Dist'] * FEET_PER_METER
                    c_mask = (sub_dist >= c_start_ft) & (sub_dist <= c_end_ft)
                    
                    if c_mask.any():
                        c_x = rot_x[c_mask]
                        c_y = rot_y[c_mask]
                        ax_plan.plot(c_x, c_y, color=colors_h.get(c['Dir'], 'green'), linewidth=6, alpha=0.5, zorder=3)
                        
                        mid_i = len(c_x) // 2
                        label_x = c_x[mid_i]
                        
                        base_offset = 40 if c['Dir'] == "Right" else -80
                        if abs(label_x - last_label_x) < 0.05: 
                            base_offset += (60 * np.sign(base_offset))
                        
                        min_r_ft = c.get('Min_Radius_m', c.get('Radius_m', 0)) * FEET_PER_METER
                        annot = f"Class {c.get('Bin', 'A')}\nMin R={min_r_ft:.0f}'\nΔ={c['Delta']:.1f}°\nL={c.get('Length_m', 0)*FEET_PER_METER:.0f}'\nPC: RP {c.get('Calibrated_Start_MP', 0):.3f}\nPT: RP {c.get('Calibrated_End_MP', 0):.3f}"
                        
                        ax_plan.annotate(annot, (label_x, c_y[mid_i]), xytext=(0, base_offset), textcoords='offset points', ha='center',
                                         arrowprops=dict(arrowstyle="->"), bbox=dict(boxstyle="round", fc="white"), zorder=5)
                        last_label_x = label_x
            ax_plan.set_title("PLAN VIEW", fontweight='bold')
            ax_plan.set_ylabel("Offset (ft)")
            ax_plan.set_ylim(-200, 200) 
            ax_plan.set_xlim(mp_min, mp_max)       
            ax_plan.grid(True, linestyle='--', alpha=0.5)
            ax_plan.legend(loc='lower right', fontsize=8)
            
            if bg_img:
                ax_inset = fig.add_axes([0.85, 0.82, 0.1, 0.1], anchor='NE', zorder=1)
                ax_inset.imshow(bg_img)
                ax_inset.axis('off')
            # --- BOTTOM: PROFILE VIEW ---
            ax_prof = fig.add_subplot(gs[1])
            
            # Line characteristics
            ax_prof.plot(sub_mp, sub_elev, 'k-', linewidth=2.5, label="Proposed Profile", zorder=2)       
            ax_prof.plot(sub_mp, sub_raw_elev, color='red', linestyle='--', linewidth=1.0, label="Raw Ground", zorder=1)
            
            ax_prof.fill_between(sub_mp, sub_elev, min(sub_elev)-20, color='#e0e0e0', alpha=0.5, zorder=1)
            last_label_x = -9999
            colors_v = {'CREST': 'orange', 'SAG': 'purple'}
            if not vc.empty:
                for _, v in vc.iterrows():
                    v_start_ft = v['Start_Dist'] * FEET_PER_METER
                    v_end_ft = v['End_Dist'] * FEET_PER_METER
                    v_mask = (sub_dist >= v_start_ft) & (sub_dist <= v_end_ft)
                    
                    if v_mask.any():
                        v_d = sub_mp[v_mask]
                        v_e = sub_elev[v_mask]
                        ax_prof.plot(v_d, v_e, color=colors_v.get(v['Type'], 'gray'), linewidth=4, zorder=3)
                        
                        mid_i = len(v_d) // 2
                        label_x = v_d[mid_i]
                        
                        y_off = -80 if v['Type'] == "CREST" else 40
                        if abs(label_x - last_label_x) < 0.05: 
                            y_off += (40 * np.sign(y_off))
                            
                        annot = f"{v['Type']}\nK={v['K_Value']:.1f}\nL={v.get('Length_m', 0)*FEET_PER_METER:.0f}'\nG1={v['Grade_In']:.2f}%  G2={v['Grade_Out']:.2f}%\nPVC: RP {v.get('Calibrated_Start_MP', 0):.3f}\nPVT: RP {v.get('Calibrated_End_MP', 0):.3f}"
                        
                        ax_prof.annotate(annot, (label_x, v_e[mid_i]), xytext=(0, y_off), textcoords='offset points', ha='center',
                                         arrowprops=dict(arrowstyle="->"), bbox=dict(boxstyle="round", fc="white"), zorder=5)
                        last_label_x = label_x
            ax_prof.set_title("PROFILE VIEW", fontweight='bold')
            ax_prof.set_ylabel("Elevation (ft)")
            ax_prof.set_xlabel("Reference Point")
            
            avg_elev = np.mean(sub_elev)
            ax_prof.set_xlim(mp_min, mp_max)
            ax_prof.set_ylim(avg_elev - 100, avg_elev + 100)           
            ax_prof.grid(True, linestyle='--', alpha=0.5)
            
            # The missing profile legend is back!
            ax_prof.legend(loc='lower right', fontsize=8)
            pdf.savefig(fig)
            plt.close(fig)
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vertices_csv", required=True)
    ap.add_argument("--horizontal_csv", required=True)
    ap.add_argument("--vertical_csv", required=True)
    ap.add_argument("--pdf_out", required=True)
    ap.add_argument("--route_id", required=True)
    args = ap.parse_args()
    make_pdf(
        vertices_csv=args.vertices_csv,
        horizontal_csv=args.horizontal_csv,
        vertical_csv=args.vertical_csv,
        pdf_out=args.pdf_out,
        route_id=args.route_id
    )
    print(f"Saved PDF: {args.pdf_out}")
if __name__ == "__main__":
    main()