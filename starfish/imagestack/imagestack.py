import collections
import os
import warnings
from copy import deepcopy
from functools import partial
from itertools import product
from json import loads
from typing import (
    Any,
    Callable,
    Iterable,
    Iterator,
    List,
    Mapping,
    MutableMapping,
    MutableSequence,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import skimage.io
import xarray as xr
from scipy.ndimage.filters import gaussian_filter
from scipy.stats import scoreatpercentile
from skimage import exposure
from skimage import img_as_float32, img_as_uint
from slicedimage import (
    ImageFormat,
    Reader,
    Tile,
    TileSet,
    Writer,
)
from slicedimage.io import resolve_path_or_url
from tqdm import tqdm

from starfish.config import StarfishConfig
from starfish.experiment.builder import build_image, TileFetcher
from starfish.experiment.builder.defaultproviders import OnesTile, tile_fetcher_factory
from starfish.imagestack import indexing_utils, physical_coordinate_calculator
from starfish.imagestack.parser import TileCollectionData, TileKey
from starfish.imagestack.parser.crop import CropParameters, CroppedTileCollectionData
from starfish.imagestack.parser.numpy import NumpyData
from starfish.imagestack.parser.tileset import parse_tileset
from starfish.intensity_table.intensity_table import IntensityTable
from starfish.multiprocessing.pool import Pool
from starfish.multiprocessing.shmem import SharedMemory
from starfish.types import (
    Axes,
    Coordinates,
    LOG,
    Number,
    PHYSICAL_COORDINATE_DIMENSION,
    PhysicalCoordinateTypes,
    STARFISH_EXTRAS_KEY
)
from starfish.util.JSONenocder import LogEncoder
from ._mp_dataarray import MPDataArray
from .dataorder import AXES_DATA, N_AXES


class ImageStack:
    """
    Container for a TileSet (field of view)
    Loads configuration from StarfishConfig.

    Attributes
    ----------
    num_chs : int
        the number of channels stored in the image tensor
    num_rounds : int
        the number of imaging rounds stored in the image tensor
    num_zplanes : int
        the number of z-layers stored in the image tensor
    numpy_array : np.ndarray
        the 5-d image tensor is stored in this array
    raw_shape : Tuple[int]
        the shape of the image tensor (in integers)
    shape : Dict[str, int]
           the shape of the image tensor by categorical index (channels, imaging rounds, z-layers)

    Methods
    -------
    get_slice(selector)
        retrieve a slice of the image tensor
    set_slice(selector, data, axes=[])
        set a slice of the image tensor
    apply(func, group_by={Axes.ROUND, Axes.CH, Axes.ZPLANE},
        in_place=False, verbose=False, n_processes=None)
        split the image tensor along one or more axes and apply a function across each of the
        components to yield an image tensor
    transform(func, group_by={Axes.ROUND, Axes.CH, Axes.ZPLANE}, verbose=False,
        n_processes=None)
        split the image tensor along one or more axes and apply a function across each of the
        components. Results are returned as a List with length equal to the number of times
        the image tensor is split.
    max_proj(*dims)
        return a max projection over one or more axis of the image tensor
    show_stack(selector, color_map='gray', figure_size=(10, 10), rescale=False, p_min=None,
        p_max=None)
        show an interactive, pageable view of the image tensor, or a slice of the image tensor
    show_stack_napari(selector)
        view the selected selectore of the image tensor with Napari. Note that Napari is
        still a prototype, but does offer more performant viewing of multi-dimensional images.
        pip install napari-gui (requires 0.0.4)
    sel(indexers)
        return an ImageStack (coordinates preserved) that is the subset described
        by the indexers. The indexers can slice all 5 dimensions of the image tensor.
    export(filepath, tile_opener=None)
        save the (potentially modified) image tensor to disk
    """

    def __init__(
            self,
            tile_shape: Tuple[int, int],
            tile_data: TileCollectionData,
    ) -> None:
        self._axes_sizes = {
            Axes.ROUND: len(set(tilekey.round for tilekey in tile_data.keys())),
            Axes.CH: len(set(tilekey.ch for tilekey in tile_data.keys())),
            Axes.ZPLANE: len(set(tilekey.z for tilekey in tile_data.keys())),
        }
        self._tile_shape = tile_shape
        self._tile_data = tile_data

        # check for existing log info
        if STARFISH_EXTRAS_KEY in tile_data.extras and LOG in tile_data.extras[STARFISH_EXTRAS_KEY]:
            self._log = loads(tile_data.extras[STARFISH_EXTRAS_KEY])[LOG]
        else:
            self._log: List[dict] = list()

        data_shape: MutableSequence[int] = []
        data_dimensions: MutableSequence[str] = []
        data_tick_marks: MutableMapping[str, Sequence[int]] = dict()
        coordinates_shape: MutableSequence[int] = []
        coordinates_dimensions: MutableSequence[str] = []
        coordinates_tick_marks: MutableMapping[str, Sequence[Union[int, str]]] = dict()
        for ix in range(N_AXES):
            size_for_axis: Optional[int] = None
            dim_for_axis: Optional[Axes] = None

            for axis_name, axis_data in AXES_DATA.items():
                if ix == axis_data.order:
                    size_for_axis = self._axes_sizes[axis_name]
                    dim_for_axis = axis_name
                    break

            if size_for_axis is None or dim_for_axis is None:
                raise ValueError(
                    f"Could not find entry for the {ix}th axis in AXES_DATA")

            data_shape.append(size_for_axis)
            data_dimensions.append(dim_for_axis.value)
            data_tick_marks[dim_for_axis.value] = list(self.axis_labels(dim_for_axis))
            coordinates_shape.append(size_for_axis)
            coordinates_dimensions.append(dim_for_axis.value)
            coordinates_tick_marks[dim_for_axis.value] = list(self.axis_labels(dim_for_axis))

        data_shape.extend(self._tile_shape)
        data_dimensions.extend([Axes.Y.value, Axes.X.value])
        coordinates_shape.append(6)
        coordinates_dimensions.append(PHYSICAL_COORDINATE_DIMENSION)
        coordinates_tick_marks[PHYSICAL_COORDINATE_DIMENSION] = [
            PhysicalCoordinateTypes.X_MIN.value,
            PhysicalCoordinateTypes.X_MAX.value,
            PhysicalCoordinateTypes.Y_MIN.value,
            PhysicalCoordinateTypes.Y_MAX.value,
            PhysicalCoordinateTypes.Z_MIN.value,
            PhysicalCoordinateTypes.Z_MAX.value,
        ]
        # now that we know the tile data type (kind and size), we can allocate the data array.
        self._data = MPDataArray.from_shape_and_dtype(
            shape=data_shape,
            dtype=np.float32,
            initial_value=0,
            dims=data_dimensions,
            coords=data_tick_marks,
        )
        self._coordinates = xr.DataArray(
            np.empty(
                shape=coordinates_shape,
                dtype=np.float32,
            ),
            dims=coordinates_dimensions,
            coords=coordinates_tick_marks,
        )

        self._tiles_aligned = True
        all_selectors = list(self._iter_axes({Axes.ROUND, Axes.CH, Axes.ZPLANE}))
        first_selector = all_selectors[0]
        tile = tile_data.get_tile(r=first_selector[Axes.ROUND],
                                  ch=first_selector[Axes.CH],
                                  z=first_selector[Axes.ZPLANE])
        # only compare X,Y coords
        starting_coords = [
            tile.coordinates[Coordinates.X][0], tile.coordinates[Coordinates.X][1],
            tile.coordinates[Coordinates.Y][0], tile.coordinates[Coordinates.Y][1],
        ]
        for selector in tqdm(all_selectors):
            tile = tile_data.get_tile(
                r=selector[Axes.ROUND], ch=selector[Axes.CH], z=selector[Axes.ZPLANE])

            data = img_as_float32(tile.numpy_array)
            self.set_slice(selector=selector, data=data)
            coordinate_selector = {
                index.value: index_value
                for index, index_value in selector.items()
            }
            coordinates_values = [
                tile.coordinates[Coordinates.X][0], tile.coordinates[Coordinates.X][1],
                tile.coordinates[Coordinates.Y][0], tile.coordinates[Coordinates.Y][1],
            ]
            if starting_coords != coordinates_values:
                self._tiles_aligned = False
            if Coordinates.Z in tile.coordinates:
                coordinates_values.extend([
                    tile.coordinates[Coordinates.Z][0], tile.coordinates[Coordinates.Z][1],
                ])
            else:
                coordinates_values.extend([np.nan, np.nan])

            self._coordinates.loc[coordinate_selector] = np.array(coordinates_values)

    @staticmethod
    def _validate_data_dtype_and_range(data: Union[np.ndarray, xr.DataArray]) -> None:
        """verify that data is of dtype float32 and in range [0, 1]"""
        if data.dtype != np.float32:
            raise TypeError(
                f"ImageStack data must be of type float32, not {data.dtype}. Please convert data "
                f"using skimage.img_as_float32 prior to calling set_slice."
            )
        if np.min(data) < 0 or np.max(data) > 1:
            raise ValueError(
                f"ImageStack data must be of type float32 and in the range [0, 1]. Please convert "
                f"data using skimage.img_as_float32 prior to calling set_slice."
            )

    def __repr__(self):
        shape = ', '.join(f'{k}: {v}' for k, v in self._data.sizes.items())
        return f"<starfish.ImageStack ({shape})>"

    @classmethod
    def from_tileset(
            cls,
            tileset: TileSet,
            crop_parameters: Optional[CropParameters]=None,
    ) -> "ImageStack":
        """
        Parse a :py:class:`slicedimage.TileSet` into an ImageStack.

        Parameters
        ----------
        tileset : TileSet
            The tileset to parse.
        crop_parameters : Optional[CropParameters]


        Returns
        -------
        ImageStack :
            An ImageStack representing encapsulating the data from the TileSet.
        """
        tile_shape, tile_data = parse_tileset(tileset)
        if crop_parameters is not None:
            tile_shape = crop_parameters.crop_shape(tile_shape)
            tile_data = CroppedTileCollectionData(tile_data, crop_parameters)
        return cls(tile_shape, tile_data)

    @classmethod
    def from_url(cls, url: str, baseurl: Optional[str]):
        """
        Constructs an ImageStack object from a URL and a base URL.

        The following examples will all load from the same location:
          1. url: https://www.example.com/images/primary_images.json  baseurl: None
          2. url: https://www.example.com/images/primary_images.json  baseurl: I_am_ignored
          3. url: primary_images.json  baseurl: https://www.example.com/images
          4. url: images/primary_images.json  baseurl: https://www.example.com

        Parameters
        ----------
        url : str
            Either an absolute URL or a relative URL referring to the image to be read.
        baseurl : Optional[str]
            If url is a relative URL, then this must be provided.  If url is an absolute URL, then
            this parameter is ignored.
        """
        config = StarfishConfig()
        tileset = Reader.parse_doc(url, baseurl, backend_config=config.slicedimage)

        return cls.from_tileset(tileset)

    @classmethod
    def from_path_or_url(cls, url_or_path: str) -> "ImageStack":
        """
        Constructs an ImageStack object from an absolute URL or a filesystem path.

        The following examples will all load from the same location:
          1. url_or_path: file:///Users/starfish-user/images/primary_images.json
          2. url_or_path: /Users/starfish-user/images/primary_images.json

        Parameters
        ----------
        url_or_path : str
            Either an absolute URL or a filesystem path to an imagestack.
        """
        config = StarfishConfig()
        _, relativeurl, baseurl = resolve_path_or_url(url_or_path,
                                                      backend_config=config.slicedimage)
        return cls.from_url(relativeurl, baseurl)

    @classmethod
    def from_numpy_array(
            cls,
            array: np.ndarray,
            index_labels: Optional[Mapping[Axes, Sequence[int]]]=None,
            coordinates: Optional[xr.DataArray]=None,
    ) -> "ImageStack":
        """Create an ImageStack from a 5d numpy array with shape (n_round, n_ch, n_z, y, x)

        Parameters
        ----------
        array : np.ndarray
            5-d tensor of shape (n_round, n_ch, n_z, y, x)
        index_labels : Optional[Mapping[Axes, Sequence[int]]]
            Mapping from axes (r, ch, z) to their labels.  If this is not provided, then the axes
            will be labeled from 0..(n-1), where n=the size of the axes.
        coordinates : Optional[xr.DataArray]
            DataArray indexed by r, ch, z, with xmin, xmax, ymin, ymax, zmin, zmax as columns.  If
            this is not provided, then the ImageStack gets fake coordinates.

        Returns
        -------
        ImageStack :
            array data stored as an ImageStack

        """
        if len(array.shape) != 5:
            raise ValueError('a 5-d tensor with shape (n_round, n_ch, n_z, y, x) must be provided.')
        try:
            cls._validate_data_dtype_and_range(array)
        except TypeError:
            warnings.warn(f"ImageStack detected as {array.dtype}. Converting to float32...")
            array = img_as_float32(array)

        n_round, n_ch, n_z, height, width = array.shape

        if index_labels is None:
            index_labels = {
                Axes.ROUND: list(range(n_round)),
                Axes.CH: list(range(n_ch)),
                Axes.ZPLANE: list(range(n_z)),
            }
        else:
            assert len(index_labels[Axes.ROUND]) == n_round
            assert len(index_labels[Axes.CH]) == n_ch
            assert len(index_labels[Axes.ZPLANE]) == n_z

        tile_data = NumpyData(array, index_labels, coordinates)
        return cls(
            (height, width),
            tile_data,
        )

    @property
    def xarray(self) -> xr.DataArray:
        """Retrieves the image data as an xarray.DataArray"""
        return self._data.data

    def sel(self, indexers: Mapping[Axes, Union[int, tuple]]):
        """Given a dictionary mapping the index name to either a value or a range represented as a
        tuple, return an Imagestack with each dimension indexed accordingly

        Parameters
        ----------
        indexers : Dict[Axes, (int/tuple)]
            A dictionary of dim:index where index is the value or range to index the dimension

        Examples
        --------

        Create an Imagestack using the ``synthetic_stack`` method
            >>> from starfish import ImageStack
            >>> from starfish.types import Axes
            >>> stack = ImageStack.synthetic_stack(5, 5, 15, 200, 200)
            >>> stack
            <starfish.ImageStack (r: 5, c: 5, z: 15, y: 200, x: 200)>
            >>> stack.sel({Axes.ROUND: (1, None), Axes.CH: 0, Axes.ZPLANE: 0})
            <starfish.ImageStack (r: 4, c: 1, z: 1, y: 200, x: 200)>
            >>> stack.sel({Axes.ROUND: 0, Axes.CH: 0, Axes.ZPLANE: 1,
            ...Axes.Y: 100, Axes.X: (None, 100)})
            <starfish.ImageStack (r: 1, c: 1, z: 1, y: 1, x: 100)>
            and the imagestack's physical coordinates
            xarray also indexed and recalculated according to the x,y slicing.

        Returns
        -------
        ImageStack :
            a new image stack indexed by given value or range.
        """

        # convert indexers to Dict[str, (int/slice)] format
        selector = indexing_utils.convert_to_selector(indexers)
        indexed_data = indexing_utils.index_keep_dimensions(self.xarray, selector)
        new_coordinates = physical_coordinate_calculator.calc_new_physical_coords_array(
            self._coordinates, self.shape, selector)
        stack = self.from_numpy_array(
            indexed_data.data,
            {
                Axes.ROUND: indexed_data[Axes.ROUND.value].values.tolist(),
                Axes.CH: indexed_data[Axes.CH.value].values.tolist(),
                Axes.ZPLANE: indexed_data[Axes.ZPLANE.value].values.tolist(),
            },
            new_coordinates,
        )
        return stack

    def get_slice(
            self,
            selector: Mapping[Axes, Union[int, slice]]
    ) -> Tuple[np.ndarray, Sequence[Axes]]:
        """
        Given a dictionary mapping the index name to either a value or a slice range, return a
        numpy array representing the slice, and a list of the remaining axes beyond the normal x-y
        tile.

        Examples
        --------

        Slicing with a scalar
            >>> from starfish import ImageStack
            >>> from starfish.types import Axes
            >>> stack = ImageStack.synthetic_stack(3, 4, 5, 20, 10)
            >>> stack.shape
            OrderedDict([(<Axes.ROUND: 'r'>, 3),
             (<Axes.CH: 'c'>, 4),
             (<Axes.ZPLANE: 'z'>, 5),
             ('y', 20),
             ('x', 10)])
            >>> stack.axis_labels(Axes.ROUND)
            [0, 1, 2]
            >>> stack.axis_labels(Axes.CH)
            [0, 1, 2, 3]
            >>> stack.axis_labels(Axes.ZPLANE)
            [2, 3, 4, 5, 6]
            >>> data, axes = stack.get_slice({Axes.ZPLANE: 6})
            >>> data.shape
            (3, 4, 20, 10)
            >>> axes
            [<Axes.ROUND: 'r'>, <Axes.CH: 'c'>]

        Slicing with a range
            >>> from starfish import ImageStack
            >>> from starfish.types import Axes
            >>> stack = ImageStack.synthetic_stack(3, 4, 5, 20, 10)
            >>> stack.shape
            OrderedDict([(<Axes.ROUND: 'r'>, 3),
             (<Axes.CH: 'c'>, 4),
             (<Axes.ZPLANE: 'z'>, 5),
             ('y', 20),
             ('x', 10)])
            >>> stack.axis_labels(Axes.ROUND)
            [0, 1, 2]
            >>> stack.axis_labels(Axes.CH)
            [0, 1, 2, 3]
            >>> stack.axis_labels(Axes.ZPLANE)
            [2, 3, 4, 5, 6]
            >>> data, axes = stack.get_slice({Axes.ZPLANE: 5, Axes.CH: slice(2, 4)})
            >>> data.shape
            (3, 2, 20, 10)
            >>> axes
            [<Axes.ROUND: 'r'>, <Axes.CH: 'c'>]
        """
        formatted_indexers = indexing_utils.convert_to_selector(selector)
        _, axes = self._build_slice_list(selector)
        result = self._data.sel(formatted_indexers).values

        if result.dtype != np.float32:
            warnings.warn(
                f"Non-float32 dtype: {result.dtype} detected. Data has likely been set using "
                f"private attributes of ImageStack. ImageStack only supports float data in the "
                f"range [0, 1]. Many algorithms will not function properly if provided other "
                f"DataTypes. See: http://scikit-image.org/docs/dev/user_guide/data_types.html")

        return result, axes

    def set_slice(
            self,
            selector: Mapping[Axes, Union[int, slice]],
            data: np.ndarray,
            axes: Optional[Sequence[Axes]]=None):
        """
        Given a dictionary mapping the index name to either a value or a slice range and a source
        numpy array, set the slice of the array of this ImageStack to the values in the source
        numpy array.

        Consumers of this API should not be aware of the internal order of the axes in ImageStack.
        As a result, they should be explicitly providing the order of the axes of the numpy array.
        This method will reorder the data to the internal order of the axes in ImageStack before
        writing it.

        Parameters
        ----------
        selector : Mapping[Axes, Union[int, slice]]
            The slice of the data we are writing with this operation.  Each index should map to a
            value or a range.  If the index is not present, we are writing to the entire range along
            that index.
        data : np.ndarray
            a 2- to 5-D numpy array containing the source data for the operation whose last two axes
            must be in (Y, X) order. If data larger than 2-D is provided, axes must be set to
            specify the order of the additional axes (see below).
        axes : Optional[Sequence[Axes]]
            The order of the axes for the source data, excluding (Y, X). Optional ONLY if data is
            a (Y, X) 2-d tile.

        Examples
        --------
        Setting a slice indicated by scalars.

            >>> import numpy as np
            >>> from starfish import ImageStack
            >>> from starfish.types import Axes
            >>> stack = ImageStack.synthetic_stack(3, 4, 5, 20, 10)
            >>> stack.shape
            OrderedDict([(<Axes.ROUND: 'r'>, 3),
             (<Axes.CH: 'c'>, 4),
             (<Axes.ZPLANE: 'z'>, 5),
             ('y', 20),
             ('x', 10)])
            >>> new_data = np.zeros((3, 4, 10, 20), dtype=np.float32)
            >>> stack.set_slice(new_data, axes=[Axes.ROUND, Axes.CH]

        Setting a slice indicated by scalars.  The data presented has a different axis order than
        the previous example.

            >>> import numpy as np
            >>> from starfish import ImageStack
            >>> from starfish.types import Axes
            >>> stack = ImageStack.synthetic_stack(3, 4, 5, 20, 10)
            >>> stack.shape
            OrderedDict([(<Axes.ROUND: 'r'>, 3),
             (<Axes.CH: 'c'>, 4),
             (<Axes.ZPLANE: 'z'>, 5),
             ('y', 20),
             ('x', 10)])
            >>> new_data = np.zeros((4, 3, 10, 20), dtype=np.float32)
            >>> stack.set_slice(new_data, axes=[Axes.CH, Axes.ROUND]

        Setting a slice indicated by a range.

            >>> from starfish import ImageStack
            >>> from starfish.types import Axes
            >>> stack = ImageStack.synthetic_stack(3, 4, 5, 20, 10)
            >>> stack.shape
            OrderedDict([(<Axes.ROUND: 'r'>, 3),
             (<Axes.CH: 'c'>, 4),
             (<Axes.ZPLANE: 'z'>, 5),
             ('y', 20),
             ('x', 10)])
            >>> new_data = np.zeros((3, 2, 10, 20), dtype=np.float32)
            >>> stack.set_slice({Axes.ZPLANE: 5, Axes.CH: slice(2, 4)}, new_data)
        """

        self._validate_data_dtype_and_range(data)

        slice_list, expected_axes = self._build_slice_list(selector)

        if axes is None:
            axes = list()
        if len(axes) != len(data.shape) - 2:
            raise ValueError(
                "data shape ({}) should be the axes ({}) and (Y,X).".format(data.shape, axes))
        move_src = list()
        move_dst = list()
        for src_idx, axis in enumerate(axes):
            try:
                dst_idx = expected_axes.index(axis)
            except ValueError:
                raise ValueError(
                    "Unexpected axis {}.  Expecting only {}.".format(axis, expected_axes))
            if src_idx != dst_idx:
                move_src.append(src_idx)
                move_dst.append(dst_idx)

        if len(move_src) != 0:
            data = np.moveaxis(data, move_src, move_dst)

        if self._data.loc[slice_list].shape != data.shape:
            raise ValueError("source shape {} mismatches destination shape {}".format(
                data.shape, self._data[slice_list].shape))

        self._data.loc[slice_list] = data

    def _get_scaled_clipped_linear_view(self, selector, rescale, p_min, p_max):

        # get the requested chunk, linearize the remaining data into a sequence of tiles
        data, remaining_inds = self.get_slice(selector)

        # identify the dimensionality of data with all dimensions other than x, y linearized
        if len(data.shape) >= 3:
            n_tiles = np.product(data.shape[:-2])
        else:
            raise ValueError(
                f'a stack with dimensionality >= 3 is required, the provided indexer produced a '
                f'stack with shape {data.shape}')

        # linearize the array
        linear_view: np.ndarray = data.reshape((n_tiles,) + data.shape[-2:])

        # set the labels for the linearized tiles
        labels: List[List[str]] = []
        for index, size in zip(remaining_inds, data.shape[:-2]):
            labels.append([f'{index}{n}' for n in range(size)])

        # mypy thinks this has an incompatible type "Iterator[Tuple[Any, ...]]";
        # it expects "Iterable[List[str]]"
        labels = list(product(*labels))  # type: ignore

        n_tiles = linear_view.shape[0]

        if rescale and any((p_min, p_max)):
            raise ValueError('select one of rescale and p_min/p_max to rescale image, not both.')

        elif rescale:
            print("Rescaling ...")
            vmin, vmax = scoreatpercentile(data, (0.5, 99.5))
            linear_view = exposure.rescale_intensity(
                linear_view,
                in_range=(vmin, vmax),
                out_range=np.float32
            ).astype(np.float32)

        elif p_min or p_max:
            print("Clipping ...")
            a_min, a_max = scoreatpercentile(
                linear_view,
                (p_min if p_min else 0, p_max if p_max else 100)
            )
            linear_view = np.clip(linear_view, a_min=a_min, a_max=a_max)

        return linear_view, labels, n_tiles

    @staticmethod
    def _show_matplotlib_notebook(
            linear_view, labels, n_tiles, figure_size, color_map
    ):
        from ipywidgets import interact, fixed

        fig, ax = plt.subplots(figsize=figure_size)
        im = ax.imshow(linear_view[0], cmap=color_map)
        ax.set_xticks([])
        ax.set_yticks([])

        def show_plane(ax, plane, plane_index, cmap="gray", title=None):
            # Update the image in the current plane
            im.set_data(plane)
            if title:
                ax.set_title(title)

        def display_slice(plane_index, ax):
            title_str = " ".join(str(lab).upper() for lab in labels[plane_index])
            show_plane(ax, linear_view[plane_index], plane_index, title=title_str, cmap=color_map)

        interact(display_slice, ax=fixed(ax), plane_index=(0, n_tiles - 1))

    @staticmethod
    def _show_matplotlib_inline(
            linear_view, labels, n_tiles, figure_size, color_map
    ):
        from ipywidgets import interact

        def show_plane(ax, plane, plane_index, cmap="gray", title=None):
            ax.imshow(plane, cmap=cmap)

            if title:
                ax.set_title(title, fontsize=16)
            ax.set_xticks([])
            ax.set_yticks([])

        @interact(plane_index=(0, n_tiles - 1))
        def display_slice(plane_index=0):
            fig, ax = plt.subplots(figsize=figure_size)
            title_str = " ".join(str(lab).upper() for lab in labels[plane_index])
            show_plane(ax, linear_view[plane_index], plane_index, title=title_str, cmap=color_map)
            plt.show()

        return display_slice

    @staticmethod
    def _build_slice_list(
            selector: Mapping[Axes, Union[int, slice]]
    ) -> Tuple[Tuple[Union[int, slice], ...], Sequence[Axes]]:
        slice_list: MutableSequence[Union[int, slice]] = [
            slice(None, None, None)
            for _ in range(N_AXES)
        ]
        axes = []
        removed_axes = set()
        for name, value in selector.items():
            idx = AXES_DATA[name].order
            if not isinstance(value, slice):
                removed_axes.add(name)
            slice_list[idx] = value

        for dimension_value, dimension_name in sorted([
            (dimension_value.order, dimension_name)
            for dimension_name, dimension_value in AXES_DATA.items()
        ]):
            if dimension_name not in removed_axes:
                axes.append(dimension_name)

        return tuple(slice_list), axes

    def _iter_axes(self, axes: Set[Axes]=None) -> Iterator[Mapping[Axes, int]]:
        """Iterate over provided axes.

        Parameters
        ----------
        axes : Set[Axes]
            The set of Axes to be iterated over (default={Axes.ROUND, Axes.CH}).

        Yields
        ------
        Dict[str, int]
            Mapping of dimension name to index

        """
        if axes is None:
            axes = {Axes.ROUND, Axes.CH}
        ordered_axes = list(axes)
        ranges = [self.axis_labels(ind) for ind in ordered_axes]
        for items in product(*ranges):
            a = zip(ordered_axes, items)
            yield {ind: val for (ind, val) in a}

    def apply(
            self,
            func: Callable,
            group_by: Set[Axes]=None,
            in_place=False,
            verbose: bool=False,
            n_processes: Optional[int]=None,
            **kwargs
    ) -> "ImageStack":
        """Split the image along a set of axes and apply a function across all the components.  This
        function should yield data of the same dimensionality as the input components.  These
        resulting components are then constituted into an ImageStack and returned.

        Parameters
        ----------
        func : Callable
            Function to apply. must expect a first argument which is a numpy array (see group_by)
            but may return any object type.
        group_by : Set[Axes]
            Axes to split the data along.  For instance, splitting a 2D array (axes: X, Y; size:
            3, 4) by X results in 3 arrays of size 4.  (default {Axes.ROUND, Axes.CH,
            Axes.ZPLANE})
        in_place : bool
            (default True) If True, function is executed in place. If n_proc is not 1, the tile or
            volume will be copied once during execution. If false, a new ImageStack object will be
            produced.
        verbose : bool
            If True, report on the percentage completed (default = False) during processing
        n_processes : Optional[int]
            The number of processes to use for apply. If None, uses the output of os.cpu_count()
            (default = None).
        kwargs : dict
            Additional arguments to pass to func

        Returns
        -------
        ImageStack :
            If inplace is False, return a new ImageStack, otherwise return a reference to the
            original stack with data modified by application of func
        """
        if group_by is None:
            group_by = {Axes.ROUND, Axes.CH, Axes.ZPLANE}

        if not in_place:
            image_stack = deepcopy(self)
            return image_stack.apply(
                func,
                group_by=group_by, in_place=True, verbose=verbose, n_processes=n_processes, **kwargs
            )
        bound_func = partial(ImageStack._in_place_apply, func)

        self.transform(
            bound_func,
            group_by=group_by,
            verbose=verbose,
            n_processes=n_processes,
            **kwargs)

        return self

    @staticmethod
    def _in_place_apply(apply_func: Callable[..., np.ndarray], data: np.ndarray, **kwargs) -> None:
        result = apply_func(data, **kwargs)
        data[:] = result

    def transform(
            self,
            func: Callable,
            group_by: Set[Axes]=None,
            verbose=False,
            n_processes: Optional[int]=None,
            **kwargs
    ) -> List[Any]:
        """Split the image along a set of axes, and apply a function across all the components.

        Parameters
        ----------
        func : Callable
            Function to apply. must expect a first argument which is a numpy array (see group_by)
            but may return any object type.
        group_by : Set[Axes]
            Axes to split the data along.  For instance, splitting a 2D array (axes: X, Y; size:
            3, 4) by X results in 3 arrays of size 4.  (default {Axes.ROUND, Axes.CH,
            Axes.ZPLANE})
        verbose : bool
            If True, report on the percentage completed (default = False) during processing
        n_processes : Optional[int]
            The number of processes to use for apply. If None, uses the output of os.cpu_count()
            (default = None).
        kwargs : dict
            Additional arguments to pass to func being applied

        Returns
        -------
        List[Any] :
            The results of applying func to stored image data
        """
        if group_by is None:
            group_by = {Axes.X, Axes.Y}

        selectors = list(self._iter_axes(group_by))
        slice_lists = [self._build_slice_list(index)[0]
                       for index in selectors]

        selectors_and_slice_lists = zip(selectors, slice_lists)
        if verbose and StarfishConfig().verbose:
            selectors_and_slice_lists = tqdm(selectors_and_slice_lists)

        with Pool(
                processes=n_processes,
                initializer=SharedMemory.initializer,
                initargs=((self._data._backing_mp_array,
                           self._data._data.shape,
                           self._data._data.dtype),)) as pool:
            mp_applyfunc: Callable = partial(
                self._processing_workflow, partial(func, **kwargs))
            results = pool.imap(mp_applyfunc, selectors_and_slice_lists)
            return list(zip(results, selectors))

    @staticmethod
    def _processing_workflow(
            worker_callable: Callable[[np.ndarray], Any],
            selector_and_slice_list: Tuple[Mapping[Axes, int],
                                           Tuple[Union[int, slice], ...]],
    ):
        backing_mp_array, shape, dtype = SharedMemory.get_payload()
        unshaped_numpy_array = np.frombuffer(backing_mp_array.get_obj(), dtype=dtype)
        numpy_array = unshaped_numpy_array.reshape(shape)

        sliced = numpy_array[selector_and_slice_list[1]]

        return worker_callable(sliced)

    @property
    def tile_metadata(self) -> pd.DataFrame:
        """return a table containing Tile metadata

        Returns
        -------
        pd.DataFrame :
            dataframe containing per-tile metadata information for each image. Guaranteed to
            include information on channel, imaging round, z plane, and barcode index. Also
            contains any information stored in the extras field for each tile.

        """

        data: collections.defaultdict = collections.defaultdict(list)
        keys = self._tile_data.keys()
        index_keys = set(
            key.value
            for key in AXES_DATA.keys()
        )
        extras_keys = set(
            key
            for tilekey in keys
            for key in self._tile_data[tilekey].keys())
        duplicate_keys = index_keys.intersection(extras_keys)
        if len(duplicate_keys) > 0:
            duplicate_keys_str = ", ".join([str(key) for key in duplicate_keys])
            raise ValueError(
                f"keys ({duplicate_keys_str}) was found in both the Tile specification and extras "
                f"field. Tile specification keys may not be duplicated in the extras field.")

        for selector in self._iter_axes({Axes.ROUND, Axes.CH, Axes.ZPLANE}):
            tilekey = TileKey(
                round=selector[Axes.ROUND],
                ch=selector[Axes.CH],
                zplane=selector[Axes.ZPLANE])
            extras = self._tile_data[tilekey]

            for index, index_value in selector.items():
                data[index.value].append(index_value)

            for k in extras_keys:
                data[k].append(extras.get(k, None))

            if 'barcode_index' not in extras:
                barcode_index = ((((selector[Axes.ZPLANE]
                                    * self.num_rounds) + selector[Axes.ROUND])
                                  * self.num_chs) + selector[Axes.CH])

                data['barcode_index'].append(barcode_index)

        return pd.DataFrame(data)

    @property
    def tiles_aligned(self) -> bool:
        """
        Returns True if all the tiles in this ImageStack have the same physical coordinates
        and False if not.
        """
        return self._tiles_aligned

    @property
    def log(self) -> List[dict]:
        """
        Returns a list of pipeline components that have been applied to this imagestack
        as well as their corresponding runtime parameters.

         ex.
            [('GaussianHighPass', {'sigma': (3, 3), 'is_volume': False}),
            ('GaussianLowPass', {'sigma': (1, 1), 'is_volume': False}])]

        Means that this imagestack was created by applying a GaussianHighPass Filter then
        a GaussianLowPass Filter to a starting imagestack

        Returns
        -------
        List[dict]
        """
        return self._log

    def update_log(self, class_instance) -> None:
        """
        Adds a new entry to the log list.

        Parameters
        ----------
        class_instance: The instance of a class being applied to the imagestack
        """
        entry = {"method": class_instance.__class__.__name__, "arguments": class_instance.__dict__}
        self._log.append(entry)

    @property
    def raw_shape(self) -> Tuple[int, int, int, int, int]:
        """
        Returns the shape of the 5-d image tensor stored as self.image

        Returns
        -------
        Tuple[int, int, int, int, int] :
            The size of the image tensor
        """
        return self._data.shape

    @property
    def shape(self) -> collections.OrderedDict:
        """
        Returns the shape of the space that this image inhabits.  It does not include the
        dimensions of the image itself.  For instance, if this is an X-Y image in a C-H-Y-X space,
        then the shape would include the axes C and H.

        Returns
        -------
        An ordered mapping between index names to the size of the index.
        """
        # TODO: (ttung) Note that the return type should be ..OrderedDict[Any, str], but python3.6
        # has a bug where this # breaks horribly.  Can't find a bug id to link to, but see
        # https://stackoverflow.com/questions/41207128/how-do-i-specify-ordereddict-k-v-types-for-\
        # mypy-type-annotation
        result: collections.OrderedDict[Any, str] = collections.OrderedDict()
        for name, data in AXES_DATA.items():
            result[name] = self._data.shape[data.order]
        result['y'] = self._data.shape[-2]
        result['x'] = self._data.shape[-1]

        return result

    @property
    def coordinates(self):
        """
        Returns an xarray where the row labels are the axes (R, C, Z) and the column labels are the
        min and max for each type of coordinate (X, Y, Z).
        """
        return self._coordinates

    def tile_coordinates(
            self,
            selector: Mapping[Axes, int],
            physical_axis: Coordinates) -> Tuple[float, float]:
        """Given a set of selector that uniquely identify a tile and a physical axis, return the min
        and the max coordinates for that tile along that axis.

        Examples
        --------
        stack.coordinates({Axes.ROUND: 4, Axes.CH: 3, Axes.ZPLANE: 2}, Coordinates.X)
            Retrieves the xmin, xmax for the tile identified by round=4, ch=3, z=2
        """

        return physical_coordinate_calculator.get_coordinates(
            coords_array=self._coordinates,
            selector=selector,
            physical_axis=physical_axis)

    @property
    def num_rounds(self):
        return self._axes_sizes[Axes.ROUND]

    @property
    def num_chs(self):
        return self._axes_sizes[Axes.CH]

    @property
    def num_zplanes(self):
        return self._axes_sizes[Axes.ZPLANE]

    AXES_TO_PROPERTY_MAP = {
        Axes.ROUND: num_rounds,
        Axes.CH: num_chs,
        Axes.ZPLANE: num_zplanes,
    }

    def axis_labels(self, axis: Axes) -> Iterable[int]:
        """Given a axis, return the sorted unique values for that axis in this ImageStack.  For
        instance, imagestack.unique_index_values(Axes.ROUND) returns all the round ids in this
        imagestack."""
        return sorted(set(tilekey[axis] for tilekey in self._tile_data.keys()))

    @property
    def tile_shape(self):
        return self._tile_shape

    def to_multipage_tiff(self, filepath: str) -> None:
        """save the ImageStack as a FIJI-compatible multi-page TIFF file

        Parameters
        ----------
        filepath : str
            filepath for a tiff FILE. "TIFF" suffix will be added if the provided path does not
            end with .TIFF

        """
        if not filepath.upper().endswith(".TIFF"):
            filepath += ".TIFF"

        # RZCYX is the order expected by FIJI
        data = self.xarray.transpose(
            Axes.ROUND.value,
            Axes.ZPLANE.value,
            Axes.CH.value,
            Axes.Y.value,
            Axes.X.value)

        # Any float32 image with low dynamic range will provoke a warning that the image is
        # low contrast because the data must be converted to uint16 for compatibility with FIJI.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            skimage.io.imsave(filepath, data.values, imagej=True)

    def export(self,
               filepath: str,
               tile_opener=None,
               tile_format: ImageFormat=ImageFormat.NUMPY) -> None:
        """write the image tensor to disk in spaceTx format

        Parameters
        ----------
        filepath : str
            Path + prefix for the images and primary_images.json written by this function
        tile_opener : TODO ttung: doc me.
        tile_format : ImageFormat
            Format in which each 2D plane should be written.

        """
        # Add log data to extras
        self._tile_data.extras[STARFISH_EXTRAS_KEY] = LogEncoder().encode({LOG: self.log})
        tileset = TileSet(
            dimensions={
                Axes.ROUND,
                Axes.CH,
                Axes.ZPLANE,
                Axes.Y,
                Axes.X,
            },
            shape={
                Axes.ROUND: self.num_rounds,
                Axes.CH: self.num_chs,
                Axes.ZPLANE: self.num_zplanes,
            },
            default_tile_shape=self._tile_shape,
            extras=self._tile_data.extras,
        )
        for tilekey in self._tile_data.keys():
            round_, ch, zplane = tilekey.round, tilekey.ch, tilekey.z
            extras: dict = self._tile_data[tilekey]

            selector = {
                Axes.ROUND: round_,
                Axes.CH: ch,
                Axes.ZPLANE: zplane,
            }

            coordinates: MutableMapping[Coordinates, Tuple[Number, Number]] = dict()
            x_coordinates = self.tile_coordinates(selector, Coordinates.X)
            y_coordinates = self.tile_coordinates(selector, Coordinates.Y)
            z_coordinates = self.tile_coordinates(selector, Coordinates.Z)

            coordinates[Coordinates.X] = x_coordinates
            coordinates[Coordinates.Y] = y_coordinates
            if z_coordinates[0] != np.nan and z_coordinates[1] != np.nan:
                coordinates[Coordinates.Z] = z_coordinates

            tile = Tile(
                coordinates=coordinates,
                indices=selector,
                extras=extras,
            )
            tile.numpy_array, _ = self.get_slice(
                selector={Axes.ROUND: round_, Axes.CH: ch, Axes.ZPLANE: zplane}
            )
            tileset.add_tile(tile)

        if tile_opener is None:
            def tile_opener(tileset_path, tile, ext):
                tile_basename = os.path.splitext(tileset_path)[0]
                if Axes.ZPLANE in tile.indices:
                    zval = tile.indices[Axes.ZPLANE]
                    zstr = "-Z{}".format(zval)
                else:
                    zstr = ""
                return open(
                    "{}-H{}-C{}{}.{}".format(
                        tile_basename,
                        tile.indices[Axes.ROUND],
                        tile.indices[Axes.CH],
                        zstr,
                        ext,
                    ),
                    "wb")

        if not filepath.endswith('.json'):
            filepath += '.json'
        Writer.write_to_path(
            tileset,
            filepath,
            pretty=True,
            tile_opener=tile_opener,
            tile_format=tile_format)

    def max_proj(self, *dims: Axes) -> "ImageStack":
        """return a max projection over one or more axis of the image tensor

        Parameters
        ----------
        dims : Axes
            one or more axes to project over

        Returns
        -------
        np.ndarray :
            max projection

        """
        max_projection = self._data.max([dim.value for dim in dims])
        max_projection = max_projection.expand_dims(tuple(dim.value for dim in dims))
        max_projection = max_projection.transpose(*self.xarray.dims)
        max_proj_stack = self.from_numpy_array(max_projection.values)
        return max_proj_stack

    def _squeezed_numpy(self, *dims: Axes):
        """return this ImageStack's data as a squeezed numpy array"""
        return self.xarray.squeeze(tuple(dim.value for dim in dims)).values

    @classmethod
    def synthetic_stack(
            cls,
            num_round: int=4,
            num_ch: int=4,
            num_z: int=12,
            tile_height: int=50,
            tile_width: int=40,
            tile_fetcher: TileFetcher=None,
    ) -> "ImageStack":
        """generate a synthetic ImageStack

        Returns
        -------
        ImageStack :
            imagestack containing a tensor whose default shape is (2, 3, 4, 30, 20)
            and whose default values are all 1.

        """
        if tile_fetcher is None:
            tile_fetcher = tile_fetcher_factory(
                OnesTile,
                False,
                (tile_height, tile_width),
            )

        collection = build_image(
            range(1),
            range(num_round),
            range(num_ch),
            range(num_z),
            tile_fetcher,
        )
        tileset = list(collection.all_tilesets())[0][1]

        return cls.from_tileset(tileset)

    @classmethod
    def synthetic_spots(
            cls, intensities: IntensityTable, num_z: int, height: int, width: int,
            n_photons_background=1000, point_spread_function=(4, 2, 2),
            camera_detection_efficiency=0.25, background_electrons=1,
            graylevel: float=37000.0 / 2 ** 16, ad_conversion_bits=16,
    ) -> "ImageStack":
        """Generate a synthetic ImageStack from a set of Features stored in an IntensityTable

        Parameters
        ----------
        intensities : IntensityTable
            IntensityTable containing coordinates of fluorophores. Used to position and generate
            spots in the output ImageStack
        num_z : int
            Number of z-planes in the ImageStack
        height : int
            Height in pixels of the ImageStack
        width : int
            Width in pixels of the ImageStack
        n_photons_background : int
            Poisson rate for the number of background photons to add to each pixel of the image.
            Set this parameter to 0 to eliminate background.
            (default 1000)
        point_spread_function : Tuple[int]
            The width of the gaussian density wherein photons spread around their light source.
            Set to zero to eliminate this (default (4, 2, 2))
        camera_detection_efficiency : float
            The efficiency of the camera to detect light. Set to 1 to remove this filter (default
            0.25)
        background_electrons : int
            Poisson rate for the number of spurious electrons detected per pixel during image
            capture by the camera (default 1)
        graylevel : float
            The number of shades of gray displayable by the synthetic camera. Larger numbers will
            produce higher resolution images (default 37000 / 2 ** 16)
        ad_conversion_bits : int
            The number of bits used during analog to visual conversion (default 16)

        Returns
        -------
        ImageStack :
            synthetic spots

        """
        # check some params
        if not 0 < camera_detection_efficiency <= 1:
            raise ValueError(
                f'invalid camera_detection_efficiency value: {camera_detection_efficiency}. '
                f'Must be in the interval (0, 1].')

        def select_uint_dtype(array):
            """choose appropriate dtype based on values of an array"""
            max_val = np.max(array)
            for dtype in (np.uint8, np.uint16, np.uint32):
                if max_val <= np.iinfo(dtype).max:
                    return array.astype(dtype)
            raise ValueError('value exceeds dynamic range of largest skimage-supported type')

        # make sure requested dimensions are large enough to support intensity values
        axis_to_size = zip((Axes.ZPLANE.value, Axes.Y.value, Axes.X.value), (num_z, height, width))
        for axis, requested_size in axis_to_size:
            required_size = intensities.coords[axis].values.max() + 1
            if required_size > requested_size:
                raise ValueError(
                    f'locations of intensities contained in table exceed the size of requested '
                    f'axis {axis}. Required size {required_size} > {requested_size}.')

        # create an empty array of the correct size
        image = np.zeros(
            (
                intensities.sizes[Axes.ROUND.value],
                intensities.sizes[Axes.CH.value],
                num_z,
                height,
                width
            ), dtype=np.uint32
        )

        # starfish uses float images, but the logic here requires uint. We cast, and will cast back
        # at the end of the function
        intensities.values = img_as_uint(intensities)

        for ch, round_ in product(*(range(s) for s in intensities.shape[1:])):
            spots = intensities[:, ch, round_]

            # numpy deprecated casting a specific way of casting floats that is triggered in xarray
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', FutureWarning)
                values = spots.where(spots, drop=True)

            image[round_, ch, values.z, values.y, values.x] = values

        intensities.values = img_as_float32(intensities)

        # add imaging noise
        image += np.random.poisson(n_photons_background, size=image.shape).astype(np.uint32)

        # blur image over coordinates, but not over round_/channels (dim 0, 1)
        sigma = (0, 0) + point_spread_function
        image = gaussian_filter(image, sigma=sigma, mode='nearest')

        image = image * camera_detection_efficiency

        image += np.random.normal(scale=background_electrons, size=image.shape)

        # mimic analog to digital conversion
        image = (image / graylevel).astype(int).clip(0, 2 ** ad_conversion_bits)

        # clip in case we've picked up some negative values
        image = np.clip(image, 0, a_max=None)

        # set the smallest int datatype that supports the data's intensity range
        image = select_uint_dtype(image)

        # convert to float for ImageStack
        with warnings.catch_warnings():
            # possible precision loss when casting from uint to float is acceptable
            warnings.simplefilter('ignore', UserWarning)
            image = img_as_float32(image)

        return cls.from_numpy_array(image)
