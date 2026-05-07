"""Translation of gsensor/utils/calc_button_position.m."""


def calc_button_position(pos):
    button_width = 0.1
    button_height = 0.03
    button_offset_x = (pos[2] - button_width) / 2
    button_offset_y = -0.04

    button_pos = [
        pos[0] + button_offset_x,
        pos[1] + button_offset_y,
        button_width,
        button_height,
    ]
    return button_pos
