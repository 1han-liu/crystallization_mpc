"""Translation of gsensor/morphs/choose_is_full.m."""

from ..utils.create_button import create_button


def choose_is_full(I):
    is_full = None

    def draw_image_and_button(fig, I, t, ii, is_full):
        if is_full:
            full_str = "Yes"
        else:
            full_str = "No"
        str_choice = "Is full: " + full_str
        ax = nexttile(t, ii)
        title(ax, str_choice)
        imshow(I, Parent=ax)

        button = create_button(
            ax,
            str_choice,
            fig,
            lambda *_: button_callback(fig, is_full),
        )
        return ax, button

    def button_callback(fig, is_full_selected):
        nonlocal is_full
        if is_full_selected:
            full_str = "Yes"
        else:
            full_str = "No"
        is_full = is_full_selected
        uiresume(fig)
        close(fig)
        disp("Is full? " + full_str)

    screen_size = get(0, "ScreenSize")
    fig_orig = figure(
        Name="Select Corner",
        NumberTitle="off",
        Position=screen_size,
    )
    movegui(fig_orig, "center")
    ax = axes(Parent=fig_orig)
    imshow(I, Parent=ax)
    t = tiledlayout(fig_orig, 2, 2)

    _, _ = draw_image_and_button(fig_orig, I, t, 3, True)
    _, _ = draw_image_and_button(fig_orig, I, t, 4, False)

    uiwait(fig_orig)
    return is_full
