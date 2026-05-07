"""Translation of gsensor/morphs/choose_corner.m."""

from ..utils.create_button import create_button


def choose_corner(I):
    corner = None
    corners = ["A", "B", "C"]

    def draw_image_and_button(fig, t, ii, corner):
        str_choice = "Corner " + corner
        I = imread(fullfile("subroutines_gsensor", "imgs", "corner_" + corner + ".jpg"))
        ax = nexttile(t, ii)
        title(ax, str_choice)
        imshow(I, Parent=ax)

        handle_I = findobj(ax, Type="image")
        handle_I.ButtonDownFcn = lambda *_: open_full_size_image(I)

        button = create_button(
            ax,
            str_choice,
            fig,
            lambda *_: button_callback(fig, corner),
        )
        return ax, button

    def button_callback(fig, corner_selected):
        nonlocal corner
        corner = corner_selected
        uiresume(fig)
        close(fig)
        disp("Selected Corner: " + corner_selected)

    def open_full_size_image(I):
        fig_full = figure(Name="Full-sized image", NumberTitle="off")
        imshow(I, Parent=axes(Parent=fig_full))

    screen_size = get(0, "ScreenSize")
    fig = figure(
        Name="Select Corner",
        NumberTitle="off",
        Position=screen_size,
    )
    movegui(fig, "center")
    t = tiledlayout(fig, 2, len(corners))

    ax = nexttile(t, 2)
    imshow(I, Parent=ax)
    title(ax, "Raw image")

    for ii in range(len(corners)):
        _, _ = draw_image_and_button(fig, t, ii + 4, corners[ii])
    sgtitle("Enlarge image by clicking on it", FontSize=14, FontWeight="bold")

    uiwait(fig)
    return corner
