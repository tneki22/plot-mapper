from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.elements.image as st_image_module
from PIL import Image
from streamlit_drawable_canvas import st_canvas

try:
    from streamlit.elements.lib.image_utils import image_to_url as _st_image_to_url
    from streamlit.elements.lib.layout_utils import LayoutConfig
except Exception:  # pragma: no cover
    _st_image_to_url = None
    LayoutConfig = None


if not hasattr(st_image_module, "image_to_url") and _st_image_to_url and LayoutConfig:
    def _compat_image_to_url(image: Any, width: int, clamp: bool, channels: str, output_format: str, image_id: str) -> str:
        return _st_image_to_url(
            image=image,
            layout_config=LayoutConfig(width=width),
            clamp=clamp,
            channels=channels,
            output_format=output_format,
            image_id=image_id,
        )

    st_image_module.image_to_url = _compat_image_to_url

TARGET_PLOTS = 182
APP_DIR = Path(__file__).resolve().parent
ASSET_IMAGE_PATH = APP_DIR / "assets" / "plan.jpg"

STREETS = [
    "Ивана Стрешнева",
    "Екатерининская",
    "Братьев Морозовых",
    "Вишневая",
    "Парковая",
]

MODE_OPTIONS = {
    "Прямоугольник (быстро)": "rect",
    "Полигон (точки + завершение кнопкой)": "polygon",
}

TYPE_LABEL_TO_CODE = {
    "Участок": "plot",
    "Детская площадка": "playground",
    "Проезд": "road",
    "Зона отдыха": "leisure",
    "Парковка": "parking",
    "Другое": "other",
}

TYPE_DEFAULT_LABEL = {
    "playground": "Детская площадка",
    "road": "Проезд",
    "leisure": "Зона отдыха",
    "parking": "Парковка",
    "other": "Объект",
}

STATUS_OPTIONS = {
    "free": "Свободен",
    "sold": "Продан",
    "reserved": "Резерв",
}

STATUS_LABEL_TO_CODE = {label: code for code, label in STATUS_OPTIONS.items()}

# Обновленная, более яркая палитра заливки
STATUS_FILL_COLORS = {
    "free": "rgba(76, 175, 80, 0.6)",      # Полностью зеленый
    "sold": "rgba(244, 67, 54, 0.6)",      # Красный
    "reserved": "rgba(255, 193, 7, 0.6)",  # Желтый/Оранжевый
}

# Обновленная обводка
STATUS_STROKE_COLORS = {
    "free": "rgba(46, 125, 50, 0.95)",
    "sold": "rgba(183, 28, 28, 0.95)",
    "reserved": "rgba(245, 127, 23, 0.95)",
}

MAX_PLOT_CORNERS = 4
MIN_POLYGON_CORNERS = 3
CLOSE_TOLERANCE_PX = 18.0
MIN_POINT_DISTANCE_PX = 4.0
POLYGON_MATCH_GRID_PX = 3


@st.cache_resource
def load_image(path: Path) -> Image.Image:
    if not path.exists():
        raise FileNotFoundError(f"Карта не найдена: {path}")
    return Image.open(path).convert("RGB")


def init_session_state() -> None:
    defaults = {
        "features": [],
        "canvas_nonce": 0,
        "sync_suspend": False,  # Флаг для игнорирования пустого ответа от холста при перерендере
        "unsaved_polygons_live": [],
        "draft_type_label": "Участок",
        "draft_status": "free",
        "draft_street": STREETS[0],
        "draft_area": 10.5,
        "draft_note": "",
        "draft_label": "",
        "draft_number_widget": 1,
        "draft_number_pending": None,
        "last_type_code": "plot",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def next_plot_number(features: list[dict[str, Any]]) -> int:
    numbers = [int(item["number"]) for item in features if item.get("type") == "plot" and item.get("number")]
    if not numbers:
        return 1
    return max(numbers) + 1


def normalize_polygon(points: list[list[float]], image_w: int, image_h: int) -> list[list[int]]:
    normalized: list[list[int]] = []
    for point in points:
        if len(point) != 2:
            continue
        x = int(round(float(point[0])))
        y = int(round(float(point[1])))
        x = max(0, min(image_w, x))
        y = max(0, min(image_h, y))
        normalized.append([x, y])
    if len(normalized) >= 2 and normalized[0] == normalized[-1]:
        normalized.pop()
    return normalized


def feature_style(feature: dict[str, Any]) -> tuple[str, str]:
    f_type = feature.get("type")
    status = feature.get("status")

    if f_type == "plot":
        fill = STATUS_FILL_COLORS.get(status, STATUS_FILL_COLORS["free"])
        stroke = STATUS_STROKE_COLORS.get(status, STATUS_STROKE_COLORS["free"])
        return fill, stroke

    return "rgba(80,127,196,0.30)", "rgba(46,88,150,0.95)"


def to_canvas_polygon_object(
    points: list[list[int]],
    scale_x: float,
    scale_y: float,
    fill_color: str,
    stroke_color: str,
    is_saved: bool,
) -> dict[str, Any]:
    scaled_points = [[point[0] * scale_x, point[1] * scale_y] for point in points]
    min_x = min(point[0] for point in scaled_points)
    min_y = min(point[1] for point in scaled_points)
    relative_points = [{"x": round(point[0] - min_x, 2), "y": round(point[1] - min_y, 2)} for point in scaled_points]

    return {
        "type": "polygon",
        "version": "5.3.0",
        "originX": "left",
        "originY": "top",
        "left": round(min_x, 2),
        "top": round(min_y, 2),
        "points": relative_points,
        "fill": fill_color,
        "stroke": stroke_color,
        "strokeWidth": 1.6,
        "strokeLineJoin": "round",
        "selectable": False,
        "evented": False,
        "objectCaching": False,
        "mk_saved": is_saved,
    }


def build_initial_drawing_with_unsaved(
    features: list[dict[str, Any]],
    scale_x: float,
    scale_y: float,
    unsaved_polygons: list[list[list[int]]],
) -> dict[str, Any]:
    objects: list[dict[str, Any]] = []
    
    for feature in features:
        points = feature.get("polygon") or []
        if len(points) < 3:
            continue
        fill_color, stroke_color = feature_style(feature)
        objects.append(to_canvas_polygon_object(points, scale_x, scale_y, fill_color, stroke_color, is_saved=True))

    for polygon in unsaved_polygons:
        if len(polygon) < 3:
            continue
        objects.append(
            to_canvas_polygon_object(
                polygon,
                scale_x,
                scale_y,
                "rgba(241, 133, 80, 0.40)",
                "rgba(188, 78, 37, 0.95)",
                is_saved=False,
            )
        )

    return {
        "version": "5.3.0",
        "objects": objects,
    }


def distance(a: list[int] | list[float], b: list[int] | list[float]) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))


def polygon_signature(points: list[list[int]]) -> tuple[tuple[int, int], ...]:
    if len(points) < 3:
        return tuple()

    def _quantize(value: int) -> int:
        return int(round(value / POLYGON_MATCH_GRID_PX) * POLYGON_MATCH_GRID_PX)

    pts = [(_quantize(int(point[0])), _quantize(int(point[1]))) for point in points]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        return tuple()

    n = len(pts)
    rotations: list[tuple[tuple[int, int], ...]] = []
    for idx in range(n):
        rotations.append(tuple(pts[idx:] + pts[:idx]))

    rev = list(reversed(pts))
    for idx in range(n):
        rotations.append(tuple(rev[idx:] + rev[:idx]))

    return min(rotations)


def sanitize_polygon_points(
    points: list[list[int]], 
    max_corners: int | None = None, 
    is_draft: bool = False
) -> tuple[list[list[int]] | None, str | None]:
    if not points:
        return None, "Полигон пустой."

    compacted: list[list[int]] = []
    for point in points:
        if not compacted or distance(point, compacted[-1]) >= MIN_POINT_DISTANCE_PX:
            compacted.append(point)

    # Исправление бага с треугольниками:
    # Удаляем последнюю точку только если мы рисуем черновик точками (is_draft), 
    # а не конвертируем уже готовый прямоугольник.
    close_tolerance = CLOSE_TOLERANCE_PX if is_draft else MIN_POINT_DISTANCE_PX
    if len(compacted) >= 2 and distance(compacted[0], compacted[-1]) <= close_tolerance:
        compacted.pop()

    if len(compacted) < MIN_POLYGON_CORNERS:
        return None, "Для полигона нужно минимум 3 уникальные точки."

    if max_corners is not None and len(compacted) > max_corners:
        return None, f"Для участка допустимо максимум {max_corners} угла."

    return compacted, None


def parse_path_points(path_commands: list[list[Any]]) -> list[list[float]]:
    points: list[list[float]] = []
    for command in path_commands:
        if not command:
            continue
        cmd = str(command[0]).upper()
        if cmd in {"M", "L"} and len(command) >= 3:
            points.append([float(command[1]), float(command[2])])
    return points


def object_to_polygon(obj: dict[str, Any], scale_x: float, scale_y: float, image_w: int, image_h: int) -> list[list[int]]:
    obj_type = obj.get("type")

    if obj_type == "rect":
        left = float(obj.get("left", 0.0))
        top = float(obj.get("top", 0.0))
        width = float(obj.get("width", 0.0)) * float(obj.get("scaleX", 1.0))
        height = float(obj.get("height", 0.0)) * float(obj.get("scaleY", 1.0))

        canvas_points = [
            [left, top],
            [left + width, top],
            [left + width, top + height],
            [left, top + height],
        ]
        source_points = [[point[0] / scale_x, point[1] / scale_y] for point in canvas_points]
        return normalize_polygon(source_points, image_w, image_h)

    if obj_type == "polygon":
        left = float(obj.get("left", 0.0))
        top = float(obj.get("top", 0.0))
        scale_obj_x = float(obj.get("scaleX", 1.0))
        scale_obj_y = float(obj.get("scaleY", 1.0))
        points = obj.get("points") or []
        canvas_points = [
            [left + float(point.get("x", 0.0)) * scale_obj_x, top + float(point.get("y", 0.0)) * scale_obj_y]
            for point in points
        ]
        source_points = [[point[0] / scale_x, point[1] / scale_y] for point in canvas_points]
        return normalize_polygon(source_points, image_w, image_h)

    if obj_type == "path":
        left = float(obj.get("left", 0.0))
        top = float(obj.get("top", 0.0))
        scale_obj_x = float(obj.get("scaleX", 1.0))
        scale_obj_y = float(obj.get("scaleY", 1.0))
        path_points = parse_path_points(obj.get("path") or [])
        canvas_points = [
            [left + point[0] * scale_obj_x, top + point[1] * scale_obj_y] for point in path_points
        ]
        source_points = [[point[0] / scale_x, point[1] / scale_y] for point in canvas_points]
        return normalize_polygon(source_points, image_w, image_h)

    return []


def extract_unsaved_polygons(
    canvas_json: dict[str, Any] | None,
    saved_polygons: list[list[list[int]]],
    scale_x: float,
    scale_y: float,
    image_w: int,
    image_h: int,
) -> list[list[list[int]]]:
    if not canvas_json:
        return []

    saved_counter = Counter(
        polygon_signature(points)
        for points in saved_polygons
        if len(points) >= MIN_POLYGON_CORNERS
    )

    objects = canvas_json.get("objects") or []
    if not objects:
        return []

    unsaved_polygons: list[list[list[int]]] = []
    for obj in objects:
        polygon = object_to_polygon(obj, scale_x, scale_y, image_w, image_h)
        if len(polygon) < MIN_POLYGON_CORNERS:
            continue

        signature = polygon_signature(polygon)
        if signature and saved_counter.get(signature, 0) > 0:
            saved_counter[signature] -= 1
            continue

        unsaved_polygons.append(polygon)

    return unsaved_polygons


def extract_polygon_draft_points(
    canvas_json: dict[str, Any] | None,
    drawing_mode: str,
    scale_x: float,
    scale_y: float,
    image_w: int,
    image_h: int,
) -> list[list[int]]:
    if not canvas_json:
        return []

    if drawing_mode != "polygon":
        return []

    objects = canvas_json.get("objects") or []
    point_objects = [
        obj for obj in objects if str(obj.get("type")) in {"point", "circle"} and obj.get("radius") is not None
    ]
    if not point_objects:
        return []

    points: list[list[float]] = []
    for obj in point_objects:
        left = float(obj.get("left", 0.0))
        top = float(obj.get("top", 0.0))
        points.append([left / scale_x, top / scale_y])

    normalized = normalize_polygon(points, image_w, image_h)
    sanitized, _ = sanitize_polygon_points(normalized, max_corners=None, is_draft=True)
    return sanitized or normalized


def feature_title(feature: dict[str, Any]) -> str:
    if feature.get("type") == "plot":
        return f"Участок №{feature.get('number')}"
    return str(feature.get("label") or TYPE_DEFAULT_LABEL.get(feature.get("type"), "Объект"))


def export_payload(image_name: str, image_w: int, image_h: int, features: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "image": image_name,
        "image_size": {"width": image_w, "height": image_h},
        "features": features,
    }


def parse_import_payload(data: dict[str, Any], image_w: int, image_h: int) -> list[dict[str, Any]]:
    raw_features = data.get("features")
    if not isinstance(raw_features, list):
        raise ValueError("В JSON отсутствует массив features.")

    parsed: list[dict[str, Any]] = []
    for raw in raw_features:
        if not isinstance(raw, dict):
            continue

        f_type = raw.get("type")
        if f_type not in TYPE_LABEL_TO_CODE.values():
            continue

        polygon = normalize_polygon(raw.get("polygon") or [], image_w, image_h)
        if len(polygon) < 3:
            continue

        if f_type == "plot":
            if raw.get("number") is None or raw.get("street") is None or raw.get("area_m2") is None or raw.get("status") is None:
                continue
            parsed.append(
                {
                    "type": "plot",
                    "number": int(raw.get("number")),
                    "street": str(raw.get("street")),
                    "area_m2": float(raw.get("area_m2")),
                    "status": str(raw.get("status")),
                    "polygon": polygon,
                    "note": str(raw.get("note") or ""),
                }
            )
        else:
            label = str(raw.get("label") or TYPE_DEFAULT_LABEL.get(f_type, "Объект"))
            parsed.append(
                {
                    "type": f_type,
                    "label": label,
                    "polygon": polygon,
                    "number": None,
                    "street": None,
                    "area_m2": None,
                    "status": None,
                }
            )

    return parsed


def main() -> None:
    st.set_page_config(
        page_title="Разметка карты участков",
        page_icon="🗺️",
        layout="wide",
    )
    init_session_state()

    image = load_image(ASSET_IMAGE_PATH)
    image_w, image_h = image.size
    features: list[dict[str, Any]] = st.session_state.features

    plot_count = sum(1 for feature in features if feature.get("type") == "plot")
    other_count = len(features) - plot_count

    st.title("Разметка карты участков")
    st.caption(
        f"Размечено: {plot_count} из {TARGET_PLOTS} участков + {other_count} других объектов"
    )

    metric_col1, metric_col2, metric_col3 = st.columns(3)
    metric_col1.metric("Участков", plot_count)
    metric_col2.metric("Других объектов", other_count)
    metric_col3.metric("Всего объектов", len(features))

    st.sidebar.header("Разметка карты участков")
    mode_label = st.sidebar.radio("Режим рисования", list(MODE_OPTIONS.keys()), index=0)
    drawing_mode = MODE_OPTIONS[mode_label]
    canvas_drawing_mode = "point" if drawing_mode == "polygon" else drawing_mode

    zoom_percent = st.sidebar.slider("Масштаб отображения", min_value=70, max_value=140, value=100, step=5)
    scale = zoom_percent / 100
    canvas_width = int(round(image_w * scale))
    canvas_height = int(round(image_h * scale))

    st.sidebar.markdown("---")
    type_label = st.sidebar.selectbox("Тип объекта", list(TYPE_LABEL_TO_CODE.keys()), key="draft_type_label")
    type_code = TYPE_LABEL_TO_CODE[type_label]

    if st.session_state.last_type_code != type_code and type_code != "plot":
        prev_type = st.session_state.last_type_code
        prev_default = TYPE_DEFAULT_LABEL.get(prev_type, "")
        if not st.session_state.draft_label or st.session_state.draft_label == prev_default:
            st.session_state.draft_label = TYPE_DEFAULT_LABEL.get(type_code, "Объект")
    st.session_state.last_type_code = type_code

    suggested_number = next_plot_number(features)
    if st.session_state.draft_number_pending is not None:
        st.session_state.draft_number_widget = int(st.session_state.draft_number_pending)
        st.session_state.draft_number_pending = None
    elif int(st.session_state.draft_number_widget) < 1:
        st.session_state.draft_number_widget = suggested_number

    if type_code == "plot":
        st.sidebar.number_input("Номер", min_value=1, step=1, key="draft_number_widget")
        st.sidebar.selectbox("Улица", STREETS, key="draft_street")
        st.sidebar.number_input(
            "Площадь, соток",
            min_value=0.01,
            step=0.01,
            format="%.2f",
            key="draft_area",
        )

        selected_status_label = st.sidebar.segmented_control(
            "Статус",
            options=list(STATUS_LABEL_TO_CODE.keys()),
            default=STATUS_OPTIONS[st.session_state.draft_status],
            help="Выбранный вариант подсвечивается и применяется к следующему сохранению.",
        )
        if selected_status_label:
            st.session_state.draft_status = STATUS_LABEL_TO_CODE[selected_status_label]
        st.sidebar.info(f"Выбран статус: {STATUS_OPTIONS[st.session_state.draft_status]}")
        st.sidebar.text_input("Комментарий", key="draft_note")
    else:
        st.sidebar.text_input("Название объекта", key="draft_label")

    st.sidebar.markdown("---")
    save_clicked = st.sidebar.button("💾 Сохранить текущий объект", type="primary", use_container_width=True)
    undo_clicked = st.sidebar.button("↶ Отменить последний объект", use_container_width=True)
    delete_unsaved_last_clicked = st.sidebar.button("⌫ Удалить последнюю несохраненную фигуру", use_container_width=True)
    clear_unsaved_clicked = st.sidebar.button("🧹 Очистить несохраненные фигуры", use_container_width=True)

    finish_polygon_clicked = False
    cancel_polygon_input_clicked = False
    if drawing_mode == "polygon":
        st.sidebar.markdown("---")
        finish_polygon_clicked = st.sidebar.button("✅ Завершить полигон из точек", use_container_width=True)
        cancel_polygon_input_clicked = st.sidebar.button("✖ Прервать ввод полигона", use_container_width=True)
        if type_code == "plot":
            st.sidebar.caption("Поставьте 4 точки по углам участка, затем нажмите кнопку завершения.")
        else:
            st.sidebar.caption("Поставьте точки по контуру, затем нажмите кнопку завершения.")

    # Динамическая раскраска черновика в холсте
    if type_code == "plot":
        stroke_color = STATUS_STROKE_COLORS.get(st.session_state.draft_status, STATUS_STROKE_COLORS["free"])
        fill_color = STATUS_FILL_COLORS.get(st.session_state.draft_status, STATUS_FILL_COLORS["free"])
    else:
        stroke_color = "rgba(46,88,150,0.95)"
        fill_color = "rgba(80,127,196,0.30)"

    scale_x = canvas_width / image_w
    scale_y = canvas_height / image_h
    resized_image = image.resize((canvas_width, canvas_height), Image.Resampling.LANCZOS)

    initial_drawing = build_initial_drawing_with_unsaved(
        features,
        scale_x,
        scale_y,
        st.session_state.unsaved_polygons_live,
    )

    canvas_result = st_canvas(
        fill_color=fill_color,
        stroke_width=2,
        stroke_color=stroke_color,
        background_image=resized_image,
        update_streamlit=True,
        height=canvas_height,
        width=canvas_width,
        drawing_mode=canvas_drawing_mode,
        display_toolbar=True,
        initial_drawing=initial_drawing,
        point_display_radius=4,
        key=f"canvas_{st.session_state.canvas_nonce}_{zoom_percent}",
    )

    saved_polygons = [
        feature.get("polygon") or []
        for feature in st.session_state.features
        if len(feature.get("polygon") or []) >= MIN_POLYGON_CORNERS
    ]

    # Если мы принудительно обновили холст, используем наш кэш, избегая "пустого" кадра
    if st.session_state.sync_suspend:
        unsaved_polygons = st.session_state.unsaved_polygons_live
        polygon_draft_points = []
        st.session_state.sync_suspend = False
    else:
        canvas_json = canvas_result.json_data if canvas_result else None
        unsaved_polygons = extract_unsaved_polygons(
            canvas_json=canvas_json,
            saved_polygons=saved_polygons,
            scale_x=scale_x,
            scale_y=scale_y,
            image_w=image_w,
            image_h=image_h,
        )
        polygon_draft_points = extract_polygon_draft_points(
            canvas_json=canvas_json,
            drawing_mode=drawing_mode,
            scale_x=scale_x,
            scale_y=scale_y,
            image_w=image_w,
            image_h=image_h,
        )
        st.session_state.unsaved_polygons_live = unsaved_polygons

    unsaved_count = len(unsaved_polygons)
    pending_polygon = unsaved_polygons[-1] if unsaved_count > 0 else None

    if finish_polygon_clicked:
        if drawing_mode != "polygon":
            st.warning("Эта кнопка работает только в режиме полигона.")
        elif type_code == "plot" and len(polygon_draft_points) < MAX_PLOT_CORNERS:
            st.warning("Для участка поставьте 4 точки по углам, затем завершите полигон.")
        elif len(polygon_draft_points) < 3:
            st.warning("Для полигона нужно минимум 3 точки.")
        else:
            sanitized_points, sanitize_error = sanitize_polygon_points(
                polygon_draft_points,
                max_corners=MAX_PLOT_CORNERS if type_code == "plot" else None,
                is_draft=True,
            )
            if sanitize_error:
                st.warning(sanitize_error)
            elif sanitized_points is None:
                st.warning("Не удалось завершить полигон. Попробуйте расставить точки заново.")
            else:
                st.session_state.unsaved_polygons_live.append(sanitized_points)
                st.session_state.canvas_nonce += 1
                st.session_state.sync_suspend = True
                st.success(f"Полигон завершен. Вершин: {len(sanitized_points)}")
                st.rerun()

    if cancel_polygon_input_clicked:
        if drawing_mode != "polygon":
            st.warning("Эта кнопка работает только в режиме полигона.")
        else:
            st.session_state.canvas_nonce += 1
            st.session_state.sync_suspend = True
            st.success("Ввод текущего полигона прерван.")
            st.rerun()

    if delete_unsaved_last_clicked:
        if not unsaved_polygons:
            st.warning("Нет несохраненных фигур для удаления.")
        else:
            st.session_state.unsaved_polygons_live = unsaved_polygons[:-1]
            st.session_state.canvas_nonce += 1
            st.session_state.sync_suspend = True
            st.success("Последняя несохраненная фигура удалена.")
            st.rerun()

    if clear_unsaved_clicked:
        if not unsaved_polygons:
            st.warning("Нет несохраненных фигур для очистки.")
        else:
            st.session_state.unsaved_polygons_live = []
            st.session_state.canvas_nonce += 1
            st.session_state.sync_suspend = True
            st.success(f"Очищено несохраненных фигур: {len(unsaved_polygons)}")
            st.rerun()

    if pending_polygon:
        st.success(f"Фигура готова к сохранению. Вершин: {len(pending_polygon)}")
    else:
        if drawing_mode == "polygon":
            st.info("Поставьте точки по контуру и нажмите «Завершить полигон из точек», затем сохраните объект.")
        else:
            st.info("Нарисуйте объект на карте, затем нажмите кнопку сохранения в левой панели.")

    if drawing_mode == "polygon" and polygon_draft_points:
        if type_code == "plot":
            st.caption(f"Текущий черновик: {len(polygon_draft_points)} из 4 точек")
        else:
            st.caption(f"Текущий черновик полигона: {len(polygon_draft_points)} точек")

    if unsaved_count > 1:
        st.warning(
            f"На холсте {unsaved_count} несохраненных фигур. При сохранении будет взята последняя нарисованная."
        )

    st.markdown("### Легенда")
    st.markdown(
        f"""
        <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:12px;">
          <div><span style="display:inline-block;width:14px;height:14px;background:{STATUS_FILL_COLORS['sold']};border:1px solid {STATUS_STROKE_COLORS['sold']};border-radius:2px;margin-right:6px;"></span>Продан</div>
          <div><span style="display:inline-block;width:14px;height:14px;background:{STATUS_FILL_COLORS['reserved']};border:1px solid {STATUS_STROKE_COLORS['reserved']};border-radius:2px;margin-right:6px;"></span>Резерв</div>
          <div><span style="display:inline-block;width:14px;height:14px;background:{STATUS_FILL_COLORS['free']};border:1px solid {STATUS_STROKE_COLORS['free']};border-radius:2px;margin-right:6px;"></span>Свободен</div>
          <div><span style="display:inline-block;width:14px;height:14px;background:rgba(80,127,196,0.3);border:1px solid rgba(46,88,150,0.95);border-radius:2px;margin-right:6px;"></span>Не-участки</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if save_clicked:
        if not pending_polygon:
            st.error("Сначала нарисуйте фигуру на карте.")
        else:
            if type_code == "plot":
                sanitized_polygon, sanitize_error = sanitize_polygon_points(
                    pending_polygon,
                    max_corners=MAX_PLOT_CORNERS,
                    is_draft=False,
                )
                if sanitize_error:
                    st.error(sanitize_error)
                    return

                plot_number = int(st.session_state.draft_number_widget)
                existing_numbers = {
                    int(item["number"]) for item in features if item.get("type") == "plot" and item.get("number") is not None
                }
                if plot_number in existing_numbers:
                    st.error(f"Участок №{plot_number} уже существует. Укажите другой номер.")
                else:
                    new_feature = {
                        "type": "plot",
                        "number": plot_number,
                        "street": st.session_state.draft_street,
                        "area_m2": round(float(st.session_state.draft_area), 2),
                        "status": st.session_state.draft_status,
                        "polygon": sanitized_polygon,
                        "note": st.session_state.draft_note.strip(),
                    }
                    st.session_state.features.append(new_feature)
                    st.session_state.unsaved_polygons_live = []
                    st.session_state.canvas_nonce += 1
                    st.session_state.sync_suspend = True
                    st.session_state.draft_number_pending = next_plot_number(st.session_state.features)
                    st.success(f"Сохранен участок №{plot_number}.")
                    st.rerun()
            else:
                label = st.session_state.draft_label.strip() or TYPE_DEFAULT_LABEL.get(type_code, "Объект")
                new_feature = {
                    "type": type_code,
                    "label": label,
                    "polygon": pending_polygon,
                    "number": None,
                    "street": None,
                    "area_m2": None,
                    "status": None,
                }
                st.session_state.features.append(new_feature)
                st.session_state.unsaved_polygons_live = []
                st.session_state.canvas_nonce += 1
                st.session_state.sync_suspend = True
                st.success(f"Сохранен объект: {label}.")
                st.rerun()

    if undo_clicked:
        if st.session_state.features:
            removed = st.session_state.features.pop()
            st.session_state.unsaved_polygons_live = []
            st.session_state.canvas_nonce += 1
            st.session_state.sync_suspend = True
            st.success(f"Удален последний объект: {feature_title(removed)}")
            st.rerun()
        else:
            st.warning("Список объектов уже пуст.")

    st.markdown("---")
    st.subheader("Работа с JSON")

    payload = export_payload(ASSET_IMAGE_PATH.name, image_w, image_h, st.session_state.features)
    payload_json = json.dumps(payload, ensure_ascii=False, indent=2)

    uploaded_file = st.file_uploader("Загрузить сохраненный JSON", type=["json"])
    if uploaded_file is not None:
        if st.button("Загрузить JSON в текущую сессию"):
            try:
                uploaded_data = json.load(uploaded_file)
                parsed_features = parse_import_payload(uploaded_data, image_w, image_h)
                st.session_state.features = parsed_features
                st.session_state.unsaved_polygons_live = []
                st.session_state.canvas_nonce += 1
                st.session_state.sync_suspend = True
                st.session_state.draft_number_pending = next_plot_number(parsed_features)
                st.success(f"JSON загружен. Объектов: {len(parsed_features)}")
                st.rerun()
            except (ValueError, json.JSONDecodeError) as exc:
                st.error(f"Не удалось прочитать JSON: {exc}")

    st.download_button(
        label="Скачать JSON",
        data=payload_json.encode("utf-8"),
        file_name="plots.json",
        mime="application/json",
        use_container_width=False,
    )

    with st.expander("Таблица всех объектов", expanded=False):
        if not st.session_state.features:
            st.info("Пока нет сохраненных объектов.")
        else:
            rows: list[dict[str, Any]] = []
            for index, feature in enumerate(st.session_state.features, start=1):
                rows.append(
                    {
                        "ID": index,
                        "Тип": feature.get("type"),
                        "Название": feature_title(feature),
                        "Статус": STATUS_OPTIONS.get(feature.get("status"), "-"),
                        "Улица": feature.get("street") or "-",
                        "Площадь": feature.get("area_m2") or "-",
                        "Точек": len(feature.get("polygon") or []),
                    }
                )

            st.dataframe(rows, use_container_width=True, hide_index=True)
            selected_id = st.selectbox(
                "Удалить объект по ID",
                options=[row["ID"] for row in rows],
                format_func=lambda row_id: f"#{row_id} — {rows[row_id - 1]['Название']}",
            )
            if st.button("Удалить выбранный объект"):
                idx = int(selected_id) - 1
                removed = st.session_state.features.pop(idx)
                st.session_state.unsaved_polygons_live = []
                st.session_state.canvas_nonce += 1
                st.session_state.sync_suspend = True
                st.success(f"Удален объект: {feature_title(removed)}")
                st.rerun()


if __name__ == "__main__":
    main()