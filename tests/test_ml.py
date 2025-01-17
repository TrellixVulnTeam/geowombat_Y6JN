import unittest

import geowombat as gw
from geowombat.data import (
    l8_224078_20200518_points,
    l8_224078_20200518_polygons,
    l8_224078_20200518,
)
from geowombat.ml import fit, predict, fit_predict

import numpy as np
import geopandas as gpd
from sklearn.preprocessing import LabelEncoder
from sklearn_xarray.preprocessing import Featurizer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.decomposition import PCA
from sklearn.naive_bayes import GaussianNB
from sklearn.cluster import KMeans
from sklearn.model_selection import GridSearchCV, KFold
from sklearn_xarray.model_selection import CrossValidatorWrapper
from xarray import DataArray as xr_da

aoi_point = gpd.read_file(l8_224078_20200518_points)
aoi_point["lc"] = LabelEncoder().fit_transform(aoi_point.name)
aoi_point = aoi_point.drop(columns=["name"])

aoi_poly = gpd.read_file(l8_224078_20200518_polygons)
aoi_poly["lc"] = LabelEncoder().fit_transform(aoi_poly.name)
aoi_poly = aoi_poly.drop(columns=["name"])


pl_wo_feat = Pipeline(
    [
        ("scaler", StandardScaler()),
        ("pca", PCA()),
        ("clf", GaussianNB()),
    ]
)
pl_wo_feat_pca1 = Pipeline(
    [
        ("scaler", StandardScaler()),
        ("pca", PCA(n_components=1)),
        ("clf", GaussianNB()),
    ]
)

cl_wo_feat = Pipeline(
    [
        ("scaler", StandardScaler()),
        ("clf", KMeans(random_state=0)),
    ]
)


class TestConfig(unittest.TestCase):
    def test_output_values_missing(self):
        with gw.config.update(
            ref_res=300,
        ):
            with gw.open(l8_224078_20200518, nodata=0) as src:
                X, Xy, clf = fit(src, pl_wo_feat, aoi_poly, col="lc")
                y1 = predict(src, X, clf)
                y2 = fit_predict(src, pl_wo_feat, aoi_poly, col="lc")

        self.assertTrue(np.all(np.isnan(y1.values[0, 0:5, 0])))
        self.assertTrue(np.all(np.isnan(y2.values[0, 0:5, 0])))
        self.assertTrue(
            np.allclose(
                y1.values[0, -5:-1, 0], np.array([2.0, 2.0, 3.0, 3.0]), equal_nan=True
            )
        )
        self.assertTrue(
            np.allclose(
                y2.values[0, -5:-1, 0], np.array([2.0, 2.0, 3.0, 3.0]), equal_nan=True
            )
        )

    def test_output_type_attri(self):

        with gw.config.update(
            ref_res=300,
        ):
            with gw.open(l8_224078_20200518, nodata=0) as src:
                X, Xy, clf = fit(src, pl_wo_feat, aoi_poly, col="lc")
                y1 = predict(src, X, clf)
                y2 = fit_predict(src, pl_wo_feat, aoi_poly, col="lc")

        self.assertTrue(isinstance(y1, xr_da))
        self.assertTrue(isinstance(y2, xr_da))
        # self.assertTrue(isinstance(y1.chunks, tuple))
        self.assertTrue(len(y1.attrs) > 0)
        self.assertTrue(len(y2.attrs) > 0)

    def test_fitpredict_eq_fit_predict_point(self):

        with gw.config.update(
            ref_res=300,
        ):
            with gw.open(l8_224078_20200518, nodata=0) as src:
                X, Xy, clf = fit(src, pl_wo_feat, aoi_point, col="lc")
                y1 = predict(src, X, clf)
                y2 = fit_predict(src, pl_wo_feat, aoi_point, col="lc")

        self.assertTrue(np.allclose(y1.values, y2.values, equal_nan=True))

    def test_fitpredict_time_point(self):

        with gw.config.update(
            ref_res=300,
        ):
            with gw.open(
                [l8_224078_20200518, l8_224078_20200518], stack_dim="time"
            ) as src:
                y1 = fit_predict(
                    src, pl_wo_feat, aoi_point, col="lc", mask_nodataval=False
                )

        self.assertTrue(np.all(y1.sel(time=1).values == y1.sel(time=2).values))

    def test_fitpredict_eq_fit_predict_cluster(self):

        with gw.config.update(
            ref_res=300,
        ):
            with gw.open(l8_224078_20200518, nodata=0) as src:
                X, Xy, clf = fit(data=src, clf=cl_wo_feat)
                y1 = predict(src, X, clf)
                y2 = fit_predict(data=src, clf=cl_wo_feat)

        self.assertTrue(np.allclose(y1.values, y2.values, equal_nan=True))

    def test_classes_match_prediction(self):

        with gw.config.update(
            ref_res=300,
        ):
            with gw.open(l8_224078_20200518) as src:
                X, Xy, clf = fit(src, pl_wo_feat, aoi_point, col="lc")
                y1 = predict(src, X, clf)
                y2 = fit_predict(src, pl_wo_feat, aoi_point, col="lc")

        self.assertTrue(
            np.all(
                [
                    len(np.unique(y1.values)) == len(np.unique(aoi_point["lc"])),
                    len(np.unique(y2.values)) == len(np.unique(aoi_point["lc"])),
                ]
            )
        )

    def test_classes_match_prediction(self):

        with gw.config.update(
            ref_res=300,
        ):
            with gw.open(l8_224078_20200518) as src:
                X, Xy, clf = fit(src, pl_wo_feat, aoi_point, col="lc")
                y1 = predict(src, X, clf)
                y2 = fit_predict(src, pl_wo_feat, aoi_point, col="lc")

        y1values = np.unique(y1.values)
        y2values = np.unique(y2.values)

        self.assertTrue(
            np.all(
                [
                    len(y1values[np.isfinite(y1values)])
                    == len(np.unique(aoi_point["lc"])),
                    len(y2values[np.isfinite(y2values)])
                    == len(np.unique(aoi_point["lc"])),
                ]
            )
        )

    # def test_nodataval_replace(self):

    #     with gw.config.update(ref_res=300):
    #         with gw.open(l8_224078_20200518, nodata=0) as src:
    #             y1 = fit_predict(
    #                 src, pl_wo_feat, aoi_poly, col="lc", mask_nodataval=False
    #             )
    #             y2 = fit_predict(
    #                 src, pl_wo_feat, aoi_poly, col="lc", mask_nodataval=True
    #             )

    #     self.assertFalse(np.allclose(y1.values, y2.values))
    #     self.assertTrue(y1.values[1:3, 1, 0].tolist() == [0, 0])
    #     self.assertTrue(np.all(np.isnan(y2.values[1:3, 1, 0])))

    # def test_nodataval_replace2(self):

    #     with gw.config.update(ref_res=300):
    #         with gw.open(l8_224078_20200518, chunks=128) as src:
    #             y1 = fit_predict(
    #                 src, pl_wo_feat, aoi_poly, col="lc", mask_nodataval=False
    #             )
    #             y2 = fit_predict(
    #                 src, pl_wo_feat, aoi_poly, col="lc", mask_nodataval=True
    #             )

    #     self.assertTrue(~np.all(y1.values != y2.values))
    #     self.assertTrue(y1.values[1:3, 1, 0].tolist() == [0, 0])
    #     self.assertTrue(np.all(np.isnan(y2.values[1:3, 1, 0])))


# def test_fitpredict_eq_fit_predict_cluster2(self):

#     cv = CrossValidatorWrapper(KFold())
#     gridsearch = GridSearchCV(
#         pl_wo_feat, cv=cv, param_grid={"pca__n_components": [1, 2]}
#     )

#     with gw.config.update(ref_res=300):
#         with gw.open(l8_224078_20200518) as src:
#             X, Xy, clf = fit(src, pl_wo_feat, aoi_poly, col="lc")
#             gridsearch.fit(*Xy)
#             clf.set_params(**gridsearch.best_params_)
#             y1 = predict(src, X, clf)

#     self.assertTrue(
#         y1.values[-10:, 1, 0].tolist()
#         == [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
#     )


if __name__ == "__main__":
    unittest.main()
