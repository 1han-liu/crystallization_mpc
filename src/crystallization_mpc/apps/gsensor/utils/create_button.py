"""Translation of gsensor/utils/create_button.m."""

from .calc_button_position import calc_button_position


def create_button(ax, string_button, fig, button_callback):
    pos = get(ax, "Position")
    button = uicontrol(
        Style="pushbutton",
        String=string_button,
        Parent=fig,
        Units="normalized",
        Position=calc_button_position(pos),
        Callback=button_callback,
    )
    return button
