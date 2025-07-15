import os
import re
import math
import tempfile
import io
import zipfile
import json
from flask import Flask, render_template, request, send_file, after_this_request, jsonify
import ezdxf

app = Flask(__name__)

#==============================================================================
# GERENCIAMENTO DO 'BANCO DE DADOS' DE CÓDIGOS (JSON)
#==============================================================================
CODES_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'codigos_utilizados.json')

def load_codes():
    if not os.path.exists(CODES_DB_PATH): return {}
    try:
        with open(CODES_DB_PATH, 'r') as f:
            content = f.read()
            if not content: return {}
            return json.loads(content)
    except (json.JSONDecodeError, FileNotFoundError): return {}

def save_codes(codes):
    with open(CODES_DB_PATH, 'w') as f: json.dump(codes, f, indent=4)

#==============================================================================
# FUNÇÕES AUXILIARES
#==============================================================================
def get_shape_area(shape_type, params):
    area = 0
    try:
        if shape_type == 'rectangle': area = float(params.get('width', 0)) * float(params.get('height', 0))
        elif shape_type == 'circle': area = math.pi * (float(params.get('diameter', 0)) / 2)**2
        elif shape_type == 'triangle': area = 0.5 * float(params.get('triangle_base', 0)) * float(params.get('triangle_height', 0))
        elif shape_type == 'right_triangle': area = 0.5 * float(params.get('rt_base', 0)) * float(params.get('rt_height', 0))
        elif shape_type == 'trapezoid':
            large_base=float(params.get('trapezoid_large_base',0)); small_base=float(params.get('trapezoid_small_base',0)); height=float(params.get('trapezoid_height',0))
            area = ((large_base + small_base) / 2) * height
    except (ValueError, TypeError): area = 0
    return area

def setup_layers(doc, styles):
    doc.layers.new('CONTORNO', dxfattribs={'color': styles.get('layer_contour_color', 7)})
    doc.layers.new('FUROS', dxfattribs={'color': styles.get('layer_holes_color', 1)})
    if styles.get('include_text_info', False): doc.layers.new('TEXTO', dxfattribs={'color': styles.get('layer_text_color', 2)})
    # CORREÇÃO: Adiciona a camada de cotas se a opção estiver marcada
    if styles.get('include_dims', False):
        doc.layers.new('COTAS', dxfattribs={'color': styles.get('layer_text_color', 2)})

def draw_holes(msp, holes):
    if holes:
        for hole in holes: msp.add_circle(center=(hole['center_x'], hole['center_y']), radius=hole['diameter'] / 2, dxfattribs={'layer': 'FUROS'})

def add_info_text(msp, text_lines, styles):
    if text_lines:
        start_point = styles.get('text_insert_point', (0, -20))
        char_height = styles.get('char_height', 5)
        line_spacing_factor = 1.5
        for i, line in enumerate(text_lines):
            y_pos = start_point[1] - (i * char_height * line_spacing_factor)
            insert_point = (start_point[0], y_pos)
            msp.add_text(
                line,
                dxfattribs={
                    'layer': 'TEXTO', 'height': char_height,
                    'color': styles.get('layer_text_color', 2), 'insert': insert_point
                }
            )

def create_dxf_file(params, holes, styles, text_lines, shape_creator, dims_creator=None):
    doc = ezdxf.new('R2000'); setup_layers(doc, styles); msp = doc.modelspace()
    
    # Cria um novo estilo de cota do zero para garantir compatibilidade
    dimstyle_name = 'NOROACO_DIMSTYLE'
    if dimstyle_name in doc.dimstyles:
        doc.dimstyles.remove(dimstyle_name)
        
    dimstyle = doc.dimstyles.new(dimstyle_name, dxfattribs={
        'dimtxt': styles.get('char_height', 20),      # Altura do texto da cota
        'dimasz': styles.get('char_height', 20) * 0.4, # Tamanho da seta
        'dimexo': styles.get('char_height', 5) * 0.4, # Distância da linha de extensão
        'dimgap': styles.get('char_height', 5) * 0.6, # Espaçamento do texto
        'dimclrt': styles.get('layer_text_color', 7)  # Cor do texto da cota
    })

    shape_creator(msp, params, dxfattribs={'layer': 'CONTORNO'})
    draw_holes(msp, holes)
    add_info_text(msp, text_lines, styles)
    
    # Chama a função de criação de cotas se ela for fornecida
    if styles.get('include_dims') and dims_creator:
        dims_creator(msp, params, styles=styles, dxfattribs={'layer': 'COTAS', 'dimstyle': dimstyle_name})

    fd, filepath = tempfile.mkstemp(suffix=".dxf"); os.close(fd)
    doc.saveas(filepath); return filepath

#==============================================================================
# ROTAS DA APLICAÇÃO
#==============================================================================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get-next-code')
def get_next_code():
    prefix_input = request.args.get('prefix', '').strip().upper()
    if not prefix_input: return jsonify({"error": "Prefixo não pode ser vazio"}), 400
    codes = load_codes(); match = re.match(r'^(.*?)(\d*)$', prefix_input)
    prefix_text = match.group(1) if match else prefix_input
    number_from_input = int(match.group(2)) if match and match.group(2) else 0
    last_saved_number = codes.get(prefix_text, 0)
    base_number = max(number_from_input, last_saved_number)
    next_number = base_number + 1; next_code = f"{prefix_text}{next_number}"
    return jsonify({"next_code": next_code})

@app.route('/generate-dxf', methods=['POST'])
def generate_dxf():
    dxf_filepath = None
    try:
        use_code_system = request.form.get('use_code_system') == 'on'
        part_name = ""; prefix = ""; number = 0
        if use_code_system:
            part_name = request.form.get('drawing_code', '').strip().upper()
            if not part_name: return "Erro: O campo 'Código do Desenho' é obrigatório.", 400
            match = re.match(r'^(.*?)(\d+)$', part_name)
            if not match: return "Erro: O formato do código do desenho é inválido.", 400
            prefix, number_str = match.groups(); number = int(number_str)
            codes = load_codes(); last_saved_number = codes.get(prefix, 0)
            if number <= last_saved_number:
                return f"Erro: O código '{part_name}' já foi utilizado. Próximo disponível: '{prefix}{last_saved_number + 1}'.", 400
        else:
            part_name = request.form.get('custom_filename', '').strip()

        shape_type = request.form.get('shape')
        sanitized_filename = re.sub(r'[^a-zA-Z0-9_-]', '', part_name) if part_name else f"{shape_type}_sem_nome"
        
        styles = {
            'layer_contour_color': int(request.form.get('contour_color', 7)),'layer_holes_color': int(request.form.get('holes_color', 1)),
            'include_dims': request.form.get('include_dims') == 'on', 'layer_text_color': int(request.form.get('text_color', 2))
        }
        
        holes_data = []; hole_indices = [re.search(r'hole_diameter_(\d+)', key).group(1) for key in request.form if re.search(r'hole_diameter_(\d+)', key)]
        for i in hole_indices:
            try:
                holes_data.append({'diameter': float(request.form.get(f'hole_diameter_{i}')),'center_x': float(request.form.get(f'hole_x_{i}')),'center_y': float(request.form.get(f'hole_y_{i}'))})
            except (ValueError, TypeError, AttributeError): continue
        
        text_lines = []; params = {key: value for key, value in request.form.items()}
        max_dim = 0
        try:
            if shape_type == 'rectangle': max_dim = max(float(params.get('width',0)), float(params.get('height',0)))
            elif shape_type == 'circle': max_dim = float(params.get('diameter',0))
            elif shape_type == 'triangle': max_dim = max(float(params.get('triangle_base',0)), float(params.get('triangle_height',0)))
            elif shape_type == 'right_triangle': max_dim = max(float(params.get('rt_base',0)), float(params.get('rt_height',0)))
            elif shape_type == 'trapezoid': max_dim = max(float(params.get('trapezoid_large_base',0)), float(params.get('trapezoid_height',0)))
        except (ValueError, TypeError): max_dim = 200
        
        # --- AJUSTE FINO DE TAMANHO E POSIÇÃO DO TEXTO E COTAS ---
        styles['max_dim'] = max_dim if max_dim > 0 else 200
        # O divisor '25' controla a proporção. Menor divisor = texto/cotas maiores.
        styles['char_height'] = min(max(styles['max_dim'] / 25, 5), 35)
        # O valor '15' é a distância mínima garantida da cota. O multiplicador '3' controla a proporção.
        styles['dim_distance'] = max(15, styles['char_height'] * 3)
        # O multiplicador '5' controla a distância do texto abaixo da peça. Maior = mais longe.
        styles['text_insert_point'] = (0, -styles['char_height'] * 2)
        
        styles['include_text_info'] = request.form.get('include_text_info') == 'on'
        if styles['include_text_info']:
            info_part_name = part_name if part_name else "PECA_SEM_NOME"
            thickness = float(params.get('material_thickness', 0))
            density = float(params.get('material_density', 7860))
            quantity = int(params.get('part_quantity', 1))
            main_area_mm2 = get_shape_area(shape_type, params)
            volume_m3 = (main_area_mm2 / 1_000_000) * (thickness / 1_000)
            unit_weight_kg = volume_m3 * density; total_weight_kg = unit_weight_kg * quantity
            unit_weight_str = f"{unit_weight_kg:.3f}".replace('.', ','); total_weight_str = f"{total_weight_kg:.3f}".replace('.', ',')
            text_lines = [
                f"{info_part_name}",
                f"Espessura: {thickness:.2f} mm  (Qtd: {quantity:02d}x)",
                f"Peso Unitário: {unit_weight_str} Kg",
                f"Peso Total: {total_weight_str} Kg"
            ]

        shape_params_float = {k: float(v) for k,v in params.items() if v and v.replace('.', '', 1).isdigit()}
        
        shape_creators = {
            'rectangle': lambda m, p, **kw: m.add_lwpolyline([(0,0),(p['width'],0),(p['width'],p['height']),(0,p['height'])],close=True,**kw),
            'circle': lambda m, p, **kw: m.add_circle(center=(p['diameter']/2, p['diameter']/2), radius=p['diameter']/2, **kw),
            'triangle': lambda m, p, **kw: m.add_lwpolyline([(0,0),(p['triangle_base'],0),(p['triangle_base']/2,p['triangle_height'])],close=True,**kw),
            'right_triangle': lambda m, p, **kw: m.add_lwpolyline([(0,0),(p['rt_base'],0),(0,p['rt_height'])],close=True,**kw),
            'trapezoid': lambda m, p, **kw: m.add_lwpolyline([(0,0),(p['trapezoid_large_base'],0),(p['trapezoid_large_base']-((p['trapezoid_large_base']-p['trapezoid_small_base'])/2),p['trapezoid_height']),(((p['trapezoid_large_base']-p['trapezoid_small_base'])/2),p['trapezoid_height'])],close=True,**kw)
        }
        
        dims_creators = {
            'rectangle': lambda msp, p, styles, **kw: (
                msp.add_aligned_dim(p1=(0, p['height']), p2=(p['width'], p['height']), distance=styles['dim_distance'], **kw).render(),
                msp.add_aligned_dim(p1=(0, 0), p2=(0, p['height']), distance=-styles['dim_distance'], **kw).render()
            ),
            'circle': lambda msp, p, styles, **kw: msp.add_diameter_dim(center=(p['diameter']/2, p['diameter']/2), radius=p['diameter']/2, angle=135, **kw).render(),
        }
        
        if shape_type in shape_creators:
            dxf_filepath = create_dxf_file(shape_params_float, holes_data, styles, text_lines, shape_creators[shape_type], dims_creators.get(shape_type))
            download_name = f"{sanitized_filename}.dxf"
        else: return "Erro: Forma não selecionada ou inválida.", 400
        
        if use_code_system:
            codes[prefix] = number
            save_codes(codes)

        @after_this_request
        def cleanup(response):
            try:
                if dxf_filepath and os.path.exists(dxf_filepath): os.remove(dxf_filepath)
            except Exception as e: app.logger.error(f"Erro ao remover arquivo temporário: {e}")
            return response
        return send_file(dxf_filepath, as_attachment=True, download_name=download_name)
    except Exception as e:
        app.logger.error(f"Erro geral na geração: {e}")
        return "Ocorreu um erro interno. Verifique se todos os campos foram preenchidos corretamente.", 500

if __name__ == '__main__':
    app.run(debug=True)
